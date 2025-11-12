import NGAClient
import json
import argparse

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
    parser.add_argument("command", choices=["thread", "backup"], help="要执行的命令")

    # 位置参数2，存储到action
    parser.add_argument(
        "action", choices=["add", "list", "all", "sub"], help="要执行的操作"
    )

    # 各种位置无关参数

    parser.add_argument("--name", type=str, help="帖子名称")
    parser.add_argument("--tid", type=int, help="帖子tid")
    parser.add_argument("--aid", type=int, help="作者aid（可选）")
    parser.add_argument("--description", type=str, help="帖子描述（可选）")

    args = parser.parse_args()
    return args


def main():

    args = args_parse()

    if args.command == "thread":
        handle_thread_command(args)
    elif args.command == "backup":
        handle_backup_command(args)
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


def handle_backup_command(args: argparse.Namespace):
    assert args.command == "backup"
    thread_configs = NGAThreadConfigs()

    thread_tid = None
    thread_aid = None
    if args.name:
        for thread in thread_configs.get_thread_configs():
            if thread["thread_name"] == args.name:
                thread_tid = thread["tid"]
                thread_aid = thread.get("aid")
                break
        if thread_tid is None:
            print(f"未找到名称为{args.name}的帖子配置。")
            return
    elif args.tid:
        thread_tid = args.tid
        thread_aid = args.aid

    if thread_tid is None:
        print("name或tid参数必须提供其一以指定要备份的帖子。")
        return

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
    else:
        print(f"未知操作{args.action}。")


if __name__ == "__main__":
    main()
