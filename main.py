import NGAClient
import json
import argparse
import os
import re
from pathlib import Path

import config
import NGAClient
import utils

# 解析命令行
# 可用命令：
# thread add --name <name> --tid <tid> [--aid <aid>] [--description <description>]
# thread list


def args_parse():
    parser = argparse.ArgumentParser(description="NGA帖子备份器")

    # 写subparser太累人了不干了
    # 位置参数1，存储到command
    parser.add_argument(
        "command", choices=["thread", "backup", "image"], help="要执行的命令"
    )

    # 位置参数2，存储到action
    parser.add_argument(
        "action",
        choices=["add", "list", "all", "sub", "pdf", "verify"],
        help="要执行的操作",
    )

    # 各种位置无关参数

    parser.add_argument("--name", type=str, help="帖子名称")
    parser.add_argument("--tid", type=int, help="帖子tid")
    parser.add_argument("--aid", type=int, help="作者aid（可选）")
    parser.add_argument("--description", type=str, help="帖子描述（可选）")

    parser.add_argument(
        "--lou_per_pdf",
        type=int,
        default=200,
        help="每个PDF包含的楼层数（仅pdf命令有效）",
    )

    args = parser.parse_args()
    return args


def main():

    args = args_parse()

    if args.command == "thread":
        handle_thread_command(args)
    elif args.command == "backup":
        handle_backup_command(args)
    elif args.command == "image":
        handle_image_command(args)
    else:
        print("未知命令。")


from NGAThreadConfigs import NGAThreadConfigs


def handle_thread_command(args: argparse.Namespace):
    assert args.command == "thread"
    thread_configs = NGAThreadConfigs()
    if args.action == "add":
        if not args.name or not args.tid:
            print("添加帖子配置时，必须提供名称和tid。")
            return
        thread_configs.add_thread(
            thread_name=args.name,
            tid=args.tid,
            aid=args.aid,
            description=args.description,
        )
        thread_configs.save_configs()
        print(f"已添加帖子配置：{args.name} (tid: {args.tid}, aid: {args.aid})")
    elif args.action == "list":
        thread_configs = thread_configs.get_thread_configs()
        if not thread_configs:
            print("没有找到任何帖子配置。")
            return
        for thread in thread_configs:
            print(
                f"名称: {thread['thread_name']}, tid: {thread['tid']}, aid: {thread.get('aid')}, 描述: {thread.get('description','')}"
            )
    else:
        print(f"未知操作{args.action}。")


from bbcode_convert import bbcode_to_html
import bs4
from PIL import Image


def handle_backup_command(args: argparse.Namespace):
    assert args.command == "backup"
    thread_tid, thread_aid = get_tidaid(args)

    client = NGAClient.NGAClient()

    if args.action == "all":
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
                img_ext = img_url.split(".")[-1].split("?")[0]
                img_filename = f"{img_url.split('/')[-1].split('?')[0]}"
                # 修改html
                img["src"] = f"../images/{img_filename}"

                if not utils.NGA_img_link_verify(img_url):
                    print(
                        f"警告：第{item['lou']}楼的第{idx+1}张图片链接无效"
                    )

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
            f"成功下载{len(download_result['succeeded'])}个文件，失败{len(download_result['failed'])}个文件。"
        )
        for failed in download_result["failed"]:
            print(f"下载失败：{failed['url']}，保存为：{failed['save_path']}")

    elif args.action == "sub":
        utils.TODO("实现备份帖子本地没有部分的功能")
    elif args.action == "pdf":
        pdf_generate(args)
    else:
        print(f"未知操作{args.action}。")


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
    max_dimension = int(getattr(config, "PDF_SPEAKER_PORTRAIT_MAX_DIMENSION", 420))
    max_aspect_ratio = float(getattr(config, "PDF_SPEAKER_PORTRAIT_MAX_RATIO", 3.0))
    aspect_ratio = height / max(width, 1)
    if max(width, height) > max_dimension or not 0.45 <= aspect_ratio <= max_aspect_ratio:
        return False

    following_text = _get_following_visible_text(img)
    match = SPEAKER_LINE_RE.match(following_text)
    if not match:
        return False

    return _looks_like_speaker_name(match.group(1))


def _is_long_image(width: int, height: int) -> bool:
    min_width = int(getattr(config, "PDF_LONG_IMAGE_MIN_WIDTH", 800))
    min_ratio = float(getattr(config, "PDF_LONG_IMAGE_MIN_RATIO", 4.0))
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

    max_slice_ratio = float(getattr(config, "PDF_LONG_IMAGE_SLICE_RATIO", 1.35))
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
def pdf_generate(args: argparse.Namespace):
    assert args.command == "backup"
    assert args.action == "pdf"
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
    # 每pdf包含args.lou_per_pdf楼层
    lou_per_pdf = args.lou_per_pdf
    assert lou_per_pdf > 0

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
            f.write(config.HTML_STYLE)
            f.write("\n</head>\n<body>\n")
            f.write(config.HTML_PRE)
            for lou in range(start_lou, end_lou + 1):
                if lou in html_content_dict:
                    f.write(f"<h2>第{lou}楼</h2>\n")
                    f.write(html_content_dict[lou])
                    f.write("<hr/>\n")
            f.write(config.HTML_POST)
            f.write("\n</body>\n</html>\n")
        # 调用weasyprint生成pdf
        command_list.append(f'weasyprint "{pdf_html_path}" "{pdf_output_path}"')

    # 多线程调用weasyprint生成pdf
    import concurrent.futures

    with concurrent.futures.ProcessPoolExecutor() as executor:
        executor.map(os.system, command_list)
    print("PDF生成完成。")


def handle_image_command(args: argparse.Namespace):
    assert args.command == "image"
    thread_tid, thread_aid = get_tidaid(args)

    if args.action == "verify":
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

    else:
        print(f"未知操作{args.action}。")


def get_tidaid(args: argparse.Namespace) -> tuple[int | None, int | None]:
    thread_tid = None
    thread_aid = None
    thread_configs = NGAThreadConfigs()
    if args.name:
        for thread in thread_configs.get_thread_configs():
            if thread["thread_name"] == args.name:
                thread_tid = thread["tid"]
                thread_aid = thread.get("aid")
                break
        if thread_tid is None:
            print(f"未找到名称为{args.name}的帖子配置。")
            return None, None
    elif args.tid:
        thread_tid = args.tid
        thread_aid = args.aid

    if thread_tid is None:
        raise ValueError("name或tid参数必须提供其一以指定要备份的帖子。")

    return thread_tid, thread_aid


if __name__ == "__main__":
    main()
