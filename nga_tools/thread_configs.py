from __future__ import annotations

import json
from typing import Optional, TypedDict, cast

from nga_tools.config import get_config


class ThreadConfig(TypedDict):
    thread_name: str
    tid: int
    aid: Optional[int]
    description: str


def _parse_thread_config(item: object) -> Optional[ThreadConfig]:
    if not isinstance(item, dict):
        return None

    data = cast(dict[str, object], item)
    thread_name = data.get("thread_name")
    tid = data.get("tid")
    aid = data.get("aid")
    description = data.get("description", "")

    if not isinstance(thread_name, str):
        return None
    if type(tid) is not int:
        return None
    if aid is not None and type(aid) is not int:
        return None
    if not isinstance(description, str):
        description = ""

    return {
        "thread_name": thread_name,
        "tid": tid,
        "aid": aid,
        "description": description,
    }


class NGAThreadConfigs:
    def __init__(self) -> None:
        self.ThreadList: list[ThreadConfig] = []
        self.config_file_path = get_config().thread_config_file
        self.load_configs()

    def load_configs(self) -> None:
        try:
            with open(self.config_file_path, "r", encoding="utf-8") as f:
                raw_data: object = json.load(f)
        except FileNotFoundError:
            self.ThreadList = []
            return

        if not isinstance(raw_data, dict):
            self.ThreadList = []
            return

        data = cast(dict[str, object], raw_data)
        raw_thread_list = data.get("ThreadList", [])
        if not isinstance(raw_thread_list, list):
            self.ThreadList = []
            return

        thread_items = cast(list[object], raw_thread_list)
        self.ThreadList = [
            thread_config
            for item in thread_items
            if (thread_config := _parse_thread_config(item)) is not None
        ]

    def add_thread(
        self, thread_name: str, tid: int, aid: Optional[int] = None, description: str = ""
    ) -> bool:
        thread_config: ThreadConfig = {
            "thread_name": thread_name,
            "tid": tid,
            "aid": aid,
            "description": description,
        }
        for existing in self.ThreadList:
            if existing["tid"] == tid and existing.get("aid") == aid:
                print("该帖子配置已存在，跳过添加。")
                return False
        self.ThreadList.append(thread_config)
        return True

    def save_configs(self) -> None:
        data = {"ThreadList": self.ThreadList}
        with open(self.config_file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def get_thread_configs(self) -> list[ThreadConfig]:
        return self.ThreadList


def resolve_thread_target(
    name: Optional[str],
    tid: Optional[int],
    aid: Optional[int],
) -> tuple[int, Optional[int]]:
    if name:
        for thread in NGAThreadConfigs().get_thread_configs():
            if thread["thread_name"] == name:
                return thread["tid"], thread.get("aid")
        print(f"未找到名称为{name}的帖子配置。")
        raise ValueError(f"未找到名称为{name}的帖子配置。")

    if tid is not None:
        return tid, aid

    raise ValueError("name或tid参数必须提供其一以指定要备份的帖子。")
