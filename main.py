import json
import argparse
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nga_tools import utils
from nga_tools.bbcode_convert import bbcode_to_html
from nga_tools.config import get_config
from nga_tools.ngaclient import NGAClient
from nga_tools.thread_configs import NGAThreadConfigs

CommandArgs = dict[str, Any]
CommandHandler = Callable[[CommandArgs], None]
PROGRAM_USAGE = "python main.py"
HELP_FLAGS = {"-h", "--help"}
ARGPARSE_ARG_KEYS = {
    "action",
    "choices",
    "const",
    "default",
    "help",
    "metavar",
    "nargs",
    "required",
    "type",
}


def _all_actions() -> list[str]:
    actions = {
        action for action_configs in COMMANDS.values() for action in action_configs
    }
    return sorted(actions)


def _provided_arg_names(argv: list[str]) -> set[str]:
    flag_to_name = {}
    for arg_name, arg_config in ARG_DEFS.items():
        for flag in arg_config["flags"]:
            flag_to_name[flag] = arg_name

    provided_args = set()
    for token in argv:
        option_name = token.split("=", 1)[0]
        if option_name in flag_to_name:
            provided_args.add(flag_to_name[option_name])
    return provided_args


def _has_arg_value(value: Any) -> bool:
    return value is not None and value != ""


def _arg_flags(arg_name: str) -> str:
    arg_config = ARG_DEFS[arg_name]
    return ", ".join(arg_config["flags"])


def _format_arg_help(arg_name: str, action_config: dict[str, Any]) -> str:
    arg_config = ARG_DEFS[arg_name]
    flags = _arg_flags(arg_name)
    metavar = arg_config.get("metavar")
    if metavar:
        flags = f"{flags} {metavar}"
    help_text = arg_config["help"]

    default_values = action_config.get("defaults", {})
    if arg_name in default_values:
        help_text += f"（默认：{default_values[arg_name]}）"

    if arg_name in action_config.get("positive", []):
        help_text += "，必须大于0"

    return f"  {flags:<28} {help_text}"


def _required_help(action_config: dict[str, Any]) -> list[str]:
    lines = []
    required_args = action_config.get("required", [])
    required_any_args = action_config.get("required_any", [])
    if required_args:
        required = ", ".join(f"--{arg_name}" for arg_name in required_args)
        lines.append(f"  必须提供：{required}")
    if required_any_args:
        required_any = " 或 ".join(f"--{arg_name}" for arg_name in required_any_args)
        lines.append(f"  必须提供其中之一：{required_any}")
    if not lines:
        lines.append("  无")
    return lines


def _format_examples(examples: list[str]) -> list[str]:
    if not examples:
        return []
    lines = ["", "示例："]
    lines.extend(f"  {example}" for example in examples)
    return lines


def format_global_help() -> str:
    lines = [
        "NGA帖子备份器",
        "",
        f"用法：{PROGRAM_USAGE} <command> <action> [options]",
        "",
        "命令：",
    ]
    for command, action_configs in COMMANDS.items():
        lines.append(f"  {command}")
        for action, action_config in action_configs.items():
            lines.append(f"    {action:<8} {action_config['summary']}")

    lines.extend(
        [
            "",
            "查看详情：",
            f"  {PROGRAM_USAGE} <command> --help",
            f"  {PROGRAM_USAGE} <command> <action> --help",
            "",
            "常用示例：",
            f"  {PROGRAM_USAGE} thread list",
            f"  {PROGRAM_USAGE} backup all --name 帖子名",
            f"  {PROGRAM_USAGE} backup pdf --name 帖子名 --pdf_workers 2",
        ]
    )
    return "\n".join(lines)


def format_command_help(command: str) -> str:
    action_configs = COMMANDS[command]
    lines = [
        f"{command} 命令",
        "",
        f"用法：{PROGRAM_USAGE} {command} <action> [options]",
        "",
        "可用操作：",
    ]
    for action, action_config in action_configs.items():
        lines.append(f"  {action:<8} {action_config['summary']}")

    lines.extend(
        [
            "",
            "查看操作详情：",
            f"  {PROGRAM_USAGE} {command} <action> --help",
        ]
    )
    return "\n".join(lines)


def format_action_help(command: str, action: str) -> str:
    action_config = COMMANDS[command][action]
    lines = [
        f"{command} {action}",
        "",
        action_config["summary"],
        "",
        f"用法：{action_config['usage']}",
        "",
        "必需参数：",
        *_required_help(action_config),
    ]

    option_args = action_config.get("args", [])
    if option_args:
        lines.extend(["", "参数："])
        lines.extend(
            _format_arg_help(arg_name, action_config) for arg_name in option_args
        )
    else:
        lines.extend(["", "参数：", "  无"])

    lines.extend(_format_examples(action_config.get("examples", [])))
    return "\n".join(lines)


def _print_help_and_exit(raw_args: list[str]) -> None:
    if not any(token in HELP_FLAGS for token in raw_args):
        return

    positional_args = [token for token in raw_args if token not in HELP_FLAGS]
    if not positional_args:
        print(format_global_help())
        raise SystemExit(0)

    command = positional_args[0]
    if command not in COMMANDS:
        print(format_global_help())
        raise SystemExit(2)

    if len(positional_args) == 1:
        print(format_command_help(command))
        raise SystemExit(0)

    action = positional_args[1]
    if action not in COMMANDS[command]:
        available_actions = ", ".join(COMMANDS[command])
        print(
            f"未知操作组合：{command} {action}。"
            f"{command}支持的操作：{available_actions}",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        print(format_command_help(command), file=sys.stderr)
        raise SystemExit(2)

    print(format_action_help(command, action))
    raise SystemExit(0)


def _validate_args(
    parser: argparse.ArgumentParser,
    args: CommandArgs,
    provided_args: set[str],
) -> None:
    command = args["command"]
    action = args["action"]
    action_configs = COMMANDS[command]
    if action not in action_configs:
        available_actions = ", ".join(sorted(action_configs))
        parser.error(
            f"未知操作组合：{command} {action}。"
            f"{command}支持的操作：{available_actions}"
        )

    action_config = action_configs[action]
    allowed_args = set(action_config["args"])
    unused_args = sorted(provided_args - allowed_args)
    if unused_args:
        parser.error(
            f"{command} {action} 不支持参数："
            + ", ".join(f"--{arg_name}" for arg_name in unused_args)
        )

    for arg_name, default_value in action_config.get("defaults", {}).items():
        if args.get(arg_name) is None:
            args[arg_name] = default_value

    missing_args = [
        arg_name
        for arg_name in action_config.get("required", [])
        if not _has_arg_value(args.get(arg_name))
    ]
    if missing_args:
        parser.error("缺少必需参数：" + ", ".join(f"--{name}" for name in missing_args))

    required_any = action_config.get("required_any", [])
    if required_any and not any(
        _has_arg_value(args.get(name)) for name in required_any
    ):
        parser.error(
            "必须提供以下参数之一：" + ", ".join(f"--{name}" for name in required_any)
        )

    positive_args = action_config.get("positive", [])
    for arg_name in positive_args:
        value = args.get(arg_name)
        if value is not None and value <= 0:
            parser.error(f"--{arg_name}必须大于0。")


def args_parse(argv: list[str] | None = None) -> CommandArgs:
    parser = argparse.ArgumentParser(description="NGA帖子备份器", add_help=False)
    raw_args = sys.argv[1:] if argv is None else argv
    _print_help_and_exit(raw_args)

    parser.add_argument(
        "command",
        choices=sorted(COMMANDS),
        help="要执行的命令",
    )
    parser.add_argument(
        "action",
        choices=_all_actions(),
        help="要执行的操作",
    )

    for arg_config in ARG_DEFS.values():
        flags = arg_config["flags"]
        kwargs = {
            key: value
            for key, value in arg_config.items()
            if key in ARGPARSE_ARG_KEYS
        }
        parser.add_argument(*flags, **kwargs)

    args = vars(parser.parse_args(raw_args))
    _validate_args(parser, args, _provided_arg_names(raw_args))
    return args


def dispatch_command(args: CommandArgs) -> None:
    action_config = COMMANDS[args["command"]][args["action"]]
    handler: CommandHandler = action_config["handler"]
    handler(args)


def main():
    args = args_parse()
    dispatch_command(args)


def handle_thread_add(args: CommandArgs) -> None:
    thread_configs = NGAThreadConfigs()
    thread_configs.add_thread(
        thread_name=args["name"],
        tid=args["tid"],
        aid=args.get("aid"),
        description=args.get("description"),
    )
    thread_configs.save_configs()
    print(
        f"已添加帖子配置：{args['name']} "
        f"(tid: {args['tid']}, aid: {args.get('aid')})"
    )


def handle_thread_list(args: CommandArgs) -> None:
    thread_configs = NGAThreadConfigs().get_thread_configs()
    if not thread_configs:
        print("没有找到任何帖子配置。")
        return
    for thread in thread_configs:
        print(
            f"名称: {thread['thread_name']}, tid: {thread['tid']}, "
            f"aid: {thread.get('aid')}, 描述: {thread.get('description','')}"
        )


import bs4
from PIL import Image


def backup_all(args: CommandArgs) -> None:
    thread_tid, thread_aid = get_tidaid(args)

    client = NGAClient()

    folder_json = utils.get_folder(thread_tid, thread_aid, "json")
    for i in range(1, client.get_page_count(thread_tid, thread_aid) + 1):
        print(f"正在获取第{i}页...")
        page_data = client.get_page(thread_tid, thread_aid, i)
        with open(f"{folder_json}/page_{i}.json", "w", encoding="utf-8") as f:
            json.dump(page_data, f, ensure_ascii=False, indent=4)

    print("开始处理")

    folder_html = utils.get_folder(thread_tid, thread_aid, "html")
    htmls = []

    for i in range(1, client.get_page_count(thread_tid, thread_aid) + 1):
        for post in client.get_page(thread_tid, thread_aid, i)["result"]:
            post_html = bbcode_to_html(post["content"])
            with open(
                f"{folder_html}/post_{post['lou']}.html", "w", encoding="utf-8"
            ) as f:
                f.write(post_html)
            htmls.append({"lou": post["lou"], "html": post_html})
    # 按lou升序排序
    htmls.sort(key=lambda x: x["lou"])
    # 检查是否有缺失的楼层
    expected_lou = 1
    missing_lou = []
    for item in htmls:
        if item["lou"] != expected_lou:
            for i in range(expected_lou, item["lou"]):
                print(f"警告：缺失楼层{i}！")
                missing_lou.append(i)
            expected_lou = item["lou"]
        expected_lou += 1

    for i in missing_lou:
        htmls.append({"lou": i, "html": "<p><em>本楼层内容缺失。</em></p>"})
    # 重新按lou排序
    htmls.sort(key=lambda x: x["lou"])

    url_set = set()
    files_to_download = []
    # 从html中提取图片链接，准备下载
    for item in htmls:
        soup = bs4.BeautifulSoup(item["html"], "html.parser")
        imgs = soup.find_all("img")
        for idx, img in enumerate(imgs):
            img_url = img.get("src")
            if not img_url:
                continue
            img_filename = f"{img_url.split('/')[-1].split('?')[0]}"
            # 修改html
            img["src"] = f"../images/{img_filename}"

            if not utils.NGA_img_link_verify(img_url):
                print(f"警告：第{item['lou']}楼的第{idx+1}张图片链接无效")

            # 添加下载任务
            if img_url not in url_set:
                url_set.add(img_url)
                save_path = (
                    utils.get_folder(thread_tid, thread_aid, "images")
                    + f"/{img_filename}"
                )
                files_to_download.append({"url": img_url, "save_path": save_path})
        # 更新html
        item["html"] = str(soup)

    folder_html_modified = utils.get_folder(thread_tid, thread_aid, "html_modified")
    for item in htmls:
        with open(
            f"{folder_html_modified}/post_{item['lou']}.html", "w", encoding="utf-8"
        ) as f:
            f.write(item["html"])

    print(f"准备下载{len(files_to_download)}个图片文件...")
    utils.get_folder(thread_tid, thread_aid, "images")
    download_result = utils.download_files(files_to_download)
    print("图片下载完成。")
    print(
        f"成功下载{len(download_result['succeeded'])}个文件，"
        f"失败{len(download_result['failed'])}个文件。"
    )
    for failed in download_result["failed"]:
        print(f"下载失败：{failed['url']}，保存为：{failed['save_path']}")


def backup_sub(args: CommandArgs) -> None:
    get_tidaid(args)
    utils.TODO("实现备份帖子本地没有部分的功能")


SPEAKER_LINE_RE = re.compile(r"^([^\s：:][^：:]{0,15})[：:]")


def _normalize_img_classes(img: bs4.Tag) -> list[str]:
    classes = img.get("class", [])
    if isinstance(classes, str):
        return [classes]
    return list(classes)


def _get_following_visible_text(node: bs4.Tag, max_chars: int = 48) -> str:
    for sibling in node.next_siblings:
        if isinstance(sibling, bs4.NavigableString):
            text = str(sibling).strip()
        elif isinstance(sibling, bs4.Tag):
            if sibling.name == "br":
                continue
            text = sibling.get_text(" ", strip=True)
        else:
            text = ""

        if text:
            return text[:max_chars]
    return ""


def _remove_leading_breaks_after(node: bs4.Tag) -> None:
    for sibling in list(node.next_siblings):
        if isinstance(sibling, bs4.NavigableString):
            if str(sibling).strip():
                return
            continue
        if isinstance(sibling, bs4.Tag) and sibling.name == "br":
            sibling.decompose()
            continue
        return


def _get_image_size(image_path: str, image_size_cache: dict[str, tuple[int, int]]) -> tuple[int, int]:
    if image_path not in image_size_cache:
        with Image.open(image_path) as image:
            image_size_cache[image_path] = image.size
    return image_size_cache[image_path]


def _looks_like_speaker_name(speaker_name: str) -> bool:
    trimmed_name = speaker_name.strip().strip('"\'“”‘’')
    if not trimmed_name or trimmed_name.startswith(("[", "<")):
        return False

    if set(trimmed_name) <= {"?", "？", "!", "！"}:
        return True

    canonical_name = re.sub(r"[（(][^）)]*[）)]", "", trimmed_name)
    canonical_name = canonical_name.replace("/", "").replace("／", "").strip()
    if not canonical_name:
        return False

    if any("\u4e00" <= char <= "\u9fff" for char in canonical_name):
        return True

    if canonical_name.isascii():
        return not canonical_name.isupper()

    return True


def _is_speaker_portrait(img: bs4.Tag, width: int, height: int) -> bool:
    app_config = get_config()
    max_dimension = app_config.pdf_speaker_portrait_max_dimension
    max_aspect_ratio = app_config.pdf_speaker_portrait_max_ratio
    aspect_ratio = height / max(width, 1)
    if max(width, height) > max_dimension or not 0.45 <= aspect_ratio <= max_aspect_ratio:
        return False

    following_text = _get_following_visible_text(img)
    match = SPEAKER_LINE_RE.match(following_text)
    if not match:
        return False

    return _looks_like_speaker_name(match.group(1))


def _is_long_image(width: int, height: int) -> bool:
    app_config = get_config()
    min_width = app_config.pdf_long_image_min_width
    min_ratio = app_config.pdf_long_image_min_ratio
    return width >= min_width and (height / max(width, 1)) >= min_ratio


def _save_slice_image(image: Image.Image, output_path: str) -> None:
    if "A" in image.getbands():
        image.save(output_path, format="PNG", optimize=True)
        return

    image.convert("RGB").save(output_path, format="JPEG", quality=92, optimize=True)


def _slice_long_image_for_pdf(
    image_path: str,
    slice_output_dir: str,
    slice_cache: dict[str, list[str]],
) -> list[str]:
    if image_path in slice_cache:
        return slice_cache[image_path]

    max_slice_ratio = get_config().pdf_long_image_slice_ratio
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(image_path).stem)
    slice_paths: list[str] = []

    with Image.open(image_path) as image:
        width, height = image.size
        slice_height = max(1, int(width * max_slice_ratio))
        if height <= slice_height:
            slice_cache[image_path] = []
            return []

        for index, start in enumerate(range(0, height, slice_height)):
            end = min(height, start + slice_height)
            segment = image.crop((0, start, width, end))
            extension = ".png" if "A" in segment.getbands() else ".jpg"
            output_path = os.path.join(
                slice_output_dir,
                f"{safe_stem}_slice_{index:03d}{extension}",
            )
            if not os.path.exists(output_path):
                _save_slice_image(segment, output_path)
            slice_paths.append(output_path)

    slice_cache[image_path] = slice_paths
    return slice_paths


def _relative_dir_path(from_dir: str, to_path: str) -> str:
    return os.path.relpath(to_path, from_dir).replace("\\", "/")


def _replace_long_image_with_slices(
    soup: bs4.BeautifulSoup,
    img: bs4.Tag,
    slice_paths: list[str],
    html_dir: str,
) -> None:
    wrapper = soup.new_tag("div")
    wrapper["class"] = ["long-image-slices"]
    alt_text = img.get("alt", "")
    for slice_path in slice_paths:
        slice_img = soup.new_tag("img")
        slice_img["src"] = _relative_dir_path(html_dir, slice_path)
        slice_img["alt"] = alt_text
        slice_img["class"] = ["long-image-slice"]
        wrapper.append(slice_img)
    img.replace_with(wrapper)


# 调用外部weasyprint生成PDF
def pdf_generate(args: CommandArgs) -> None:
    app_config = get_config()
    thread_tid, thread_aid = get_tidaid(args)

    # 首先对图片去重，让html中同一图片指向同一文件
    folder_images = utils.get_folder(thread_tid, thread_aid, "images")
    filename_hash = {}
    hash_filename = {}
    image_files = utils.list_files_in_folder(folder_images)
    for image_file in image_files:
        image_path = f"{folder_images}/{image_file}"
        image_hash = utils.sha256(image_path)
        filename_hash[image_file] = image_hash
        if image_hash not in hash_filename:
            hash_filename[image_hash] = image_file

    # 读取modified html文件，替换图片链接
    folder_html_modified = utils.get_folder(thread_tid, thread_aid, "html_modified")
    html_files = utils.list_files_in_folder(folder_html_modified, ends_with=".html")
    folder_pdf = utils.get_folder(thread_tid, thread_aid, "pdf")
    slice_output_dir = os.path.join(folder_pdf, "long_image_slices")
    os.makedirs(slice_output_dir, exist_ok=True)

    html_content_dict = {}
    image_size_cache: dict[str, tuple[int, int]] = {}
    slice_cache: dict[str, list[str]] = {}

    for html_file in html_files:
        html_path = f"{folder_html_modified}/{html_file}"
        lou = int(html_file.split("_")[1].split(".")[0])
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        soup = bs4.BeautifulSoup(html_content, "html.parser")

        imgs = soup.find_all("img")
        for img in imgs:
            img_src = img.get("src")
            if not img_src:
                continue
            img_filename = img_src.split("/")[-1]
            if img_filename in filename_hash:
                img_hash = filename_hash[img_filename]
                canonical_filename = hash_filename[img_hash]
                canonical_path = os.path.join(folder_images, canonical_filename)
                if canonical_filename != img_filename:
                    # 替换为规范文件名
                    img["src"] = f"../images/{canonical_filename}"
            else:
                raise Exception(
                    f"HTML文件{html_file}中引用了不存在的图片文件{img_filename}！"
                )

            try:
                width, height = _get_image_size(canonical_path, image_size_cache)
            except OSError as error:
                print(f"警告：跳过无法识别尺寸的图片 {canonical_filename}: {error}")
                continue

            if _is_long_image(width, height):
                slice_paths = _slice_long_image_for_pdf(
                    canonical_path,
                    slice_output_dir,
                    slice_cache,
                )
                if slice_paths:
                    _replace_long_image_with_slices(soup, img, slice_paths, folder_pdf)
                    continue

            if _is_speaker_portrait(img, width, height):
                img_classes = _normalize_img_classes(img)
                if "speaker-portrait" not in img_classes:
                    img_classes.append("speaker-portrait")
                    img["class"] = img_classes
                _remove_leading_breaks_after(img)

        html_content_dict[lou] = str(soup)
        # 将&amp;#9834;这样的字符替换回实体字符
        html_content_dict[lou] = html_content_dict[lou].replace("&amp;#", "&#")

    # 生成中间html到pdf文件夹，然后os.system调用weasyprint生成pdf
    # 每pdf包含args["lou_per_pdf"]楼层
    lou_per_pdf = args["lou_per_pdf"]
    assert lou_per_pdf > 0
    pdf_workers = args.get("pdf_workers")
    if pdf_workers is not None and pdf_workers <= 0:
        raise ValueError("--pdf_workers必须大于0。")

    command_list = []

    for i in range(1, len(html_content_dict) // lou_per_pdf + 2):
        start_lou = (i - 1) * lou_per_pdf
        end_lou = min(i * lou_per_pdf - 1, len(html_content_dict))
        if start_lou > end_lou:
            break
        pdf_html_path = f"{folder_pdf}/part_{start_lou}_{end_lou}.html"
        pdf_output_path = f"{folder_pdf}/part_{start_lou}_{end_lou}.pdf"
        with open(pdf_html_path, "w", encoding="utf-8") as f:
            f.write("<html>\n<head>\n<meta charset=\"utf-8\"/>\n")
            f.write(app_config.html_style)
            f.write("\n</head>\n<body>\n")
            f.write(app_config.html_pre)
            for lou in range(start_lou, end_lou + 1):
                if lou in html_content_dict:
                    f.write(f"<h2>第{lou}楼</h2>\n")
                    f.write(html_content_dict[lou])
                    f.write("<hr/>\n")
            f.write(app_config.html_post)
            f.write("\n</body>\n</html>\n")
        # 调用weasyprint生成pdf
        command_list.append(f'weasyprint "{pdf_html_path}" "{pdf_output_path}"')

    # 按指定worker数量并行调用weasyprint生成pdf
    import concurrent.futures

    worker_desc = pdf_workers if pdf_workers is not None else "默认"
    print(f"开始生成{len(command_list)}个PDF，worker数量：{worker_desc}")
    with concurrent.futures.ProcessPoolExecutor(max_workers=pdf_workers) as executor:
        exit_codes = list(executor.map(os.system, command_list))
    failed_count = sum(exit_code != 0 for exit_code in exit_codes)
    if failed_count:
        raise RuntimeError(f"{failed_count}个PDF生成任务失败。")
    print("PDF生成完成。")


def image_verify(args: CommandArgs) -> None:
    thread_tid, thread_aid = get_tidaid(args)

    folder_images = utils.get_folder(thread_tid, thread_aid, "images")
    image_files = utils.list_files_in_folder(folder_images)
    print(f"已下载图片文件数：{len(image_files)}")

    for image_file in image_files:
        image_path = f"{folder_images}/{image_file}"
        try:
            with Image.open(image_path) as img:
                img.verify()  # 验证图像完整性
        except (IOError, SyntaxError) as e:
            print(f"图片文件损坏或无法打开：{image_file}，错误信息：{e}")
            # 删除损坏的文件
            os.remove(image_path)


def get_tidaid(args: CommandArgs) -> tuple[int | None, int | None]:
    thread_tid = None
    thread_aid = None
    thread_configs = NGAThreadConfigs()
    if args.get("name"):
        for thread in thread_configs.get_thread_configs():
            if thread["thread_name"] == args["name"]:
                thread_tid = thread["tid"]
                thread_aid = thread.get("aid")
                break
        if thread_tid is None:
            print(f"未找到名称为{args['name']}的帖子配置。")
            return None, None
    elif args.get("tid"):
        thread_tid = args["tid"]
        thread_aid = args.get("aid")

    if thread_tid is None:
        raise ValueError("name或tid参数必须提供其一以指定要备份的帖子。")

    return thread_tid, thread_aid


ARG_DEFS: dict[str, dict[str, Any]] = {
    "name": {
        "flags": ["--name"],
        "type": str,
        "metavar": "NAME",
        "help": "帖子名称",
    },
    "tid": {
        "flags": ["--tid"],
        "type": int,
        "metavar": "TID",
        "help": "帖子tid",
    },
    "aid": {
        "flags": ["--aid"],
        "type": int,
        "metavar": "AID",
        "help": "作者aid（可选）",
    },
    "description": {
        "flags": ["--description"],
        "type": str,
        "metavar": "TEXT",
        "help": "帖子描述（可选）",
    },
    "lou_per_pdf": {
        "flags": ["--lou_per_pdf"],
        "type": int,
        "metavar": "N",
        "help": "每个PDF包含的楼层数（仅pdf命令有效）",
    },
    "pdf_workers": {
        "flags": ["--pdf_workers"],
        "type": int,
        "metavar": "N",
        "help": "生成PDF时并行运行weasyprint的worker数量（仅pdf命令有效）",
    },
}


COMMANDS: dict[str, dict[str, dict[str, Any]]] = {
    "thread": {
        "add": {
            "handler": handle_thread_add,
            "summary": "添加帖子配置",
            "usage": (
                f"{PROGRAM_USAGE} thread add --name NAME --tid TID "
                "[--aid AID] [--description TEXT]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} thread add --name 帖子名 --tid 12345678",
                f"{PROGRAM_USAGE} thread add --name 帖子名 --tid 12345678 --aid 987654",
            ],
            "args": ["name", "tid", "aid", "description"],
            "required": ["name", "tid"],
        },
        "list": {
            "handler": handle_thread_list,
            "summary": "列出已保存的帖子配置",
            "usage": f"{PROGRAM_USAGE} thread list",
            "examples": [f"{PROGRAM_USAGE} thread list"],
            "args": [],
        },
    },
    "backup": {
        "all": {
            "handler": backup_all,
            "summary": "抓取帖子内容并下载图片",
            "usage": f"{PROGRAM_USAGE} backup all (--name NAME | --tid TID) [--aid AID]",
            "examples": [
                f"{PROGRAM_USAGE} backup all --name 帖子名",
                f"{PROGRAM_USAGE} backup all --tid 12345678 --aid 987654",
            ],
            "args": ["name", "tid", "aid"],
            "required_any": ["name", "tid"],
        },
        "sub": {
            "handler": backup_sub,
            "summary": "补充备份本地缺失内容（暂未实现）",
            "usage": f"{PROGRAM_USAGE} backup sub (--name NAME | --tid TID) [--aid AID]",
            "examples": [f"{PROGRAM_USAGE} backup sub --name 帖子名"],
            "args": ["name", "tid", "aid"],
            "required_any": ["name", "tid"],
        },
        "pdf": {
            "handler": pdf_generate,
            "summary": "根据已备份的HTML和图片生成PDF",
            "usage": (
                f"{PROGRAM_USAGE} backup pdf (--name NAME | --tid TID) [--aid AID] "
                "[--lou_per_pdf N] [--pdf_workers N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup pdf --name 帖子名",
                f"{PROGRAM_USAGE} backup pdf --name 帖子名 --lou_per_pdf 100 --pdf_workers 2",
            ],
            "args": ["name", "tid", "aid", "lou_per_pdf", "pdf_workers"],
            "required_any": ["name", "tid"],
            "defaults": {"lou_per_pdf": 200},
            "positive": ["lou_per_pdf", "pdf_workers"],
        },
    },
    "image": {
        "verify": {
            "handler": image_verify,
            "summary": "校验已下载图片，删除损坏文件",
            "usage": f"{PROGRAM_USAGE} image verify (--name NAME | --tid TID) [--aid AID]",
            "examples": [f"{PROGRAM_USAGE} image verify --name 帖子名"],
            "args": ["name", "tid", "aid"],
            "required_any": ["name", "tid"],
        },
    },
}


if __name__ == "__main__":
    main()
