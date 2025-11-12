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
        utils.TODO("实现备份帖子的全部功能")
    elif args.action == "sub":
        utils.TODO("实现备份帖子本地没有部分的功能")
    else:
        print(f"未知操作{args.action}。")


if __name__ == "__main__":
    main()
