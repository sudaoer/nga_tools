from __future__ import annotations

import json
from typing import Optional, TypeAlias, cast

from nga_tools.config import get_config


ThreadConfigValue: TypeAlias = str | int | float | bool | None
ThreadConfig: TypeAlias = dict[str, ThreadConfigValue]


def _validate_thread_config_value(
    key: str,
    value: object,
    source: object,
) -> ThreadConfigValue:
    if value is None:
        return None
    if isinstance(value, (str, float, bool)):
        return value
    if type(value) is int:
        return value
    raise ValueError(
        "帖子配置字段值必须是字符串、数字、布尔值或null，"
        f"不能是数组或对象：{key}={value!r}, source={source!r}"
    )


def _parse_thread_config(item: object) -> ThreadConfig:
    if not isinstance(item, dict):
        raise ValueError(f"帖子配置项必须是对象：{item!r}")

    data = cast(dict[str, object], item)
    source: object = data
    thread_name = data.get("thread_name")
    tid = data.get("tid")
    aid = data.get("aid")

    if not isinstance(thread_name, str):
        raise ValueError(f"帖子配置缺少字符串字段 thread_name：{source!r}")
    if type(tid) is not int:
        raise ValueError(f"帖子配置缺少整数字段 tid：{source!r}")
    if aid is not None and type(aid) is not int:
        raise ValueError(f"帖子配置字段 aid 必须是整数或null：{source!r}")

    return {
        key: _validate_thread_config_value(key, value, source)
        for key, value in data.items()
    }


def thread_config_name(thread_config: ThreadConfig) -> str:
    value = thread_config["thread_name"]
    if not isinstance(value, str):
        raise ValueError(f"帖子配置字段 thread_name 必须是字符串：{thread_config!r}")
    return value


def thread_config_tid(thread_config: ThreadConfig) -> int:
    value = thread_config["tid"]
    if type(value) is not int:
        raise ValueError(f"帖子配置字段 tid 必须是整数：{thread_config!r}")
    return value


def thread_config_aid(thread_config: ThreadConfig) -> Optional[int]:
    value = thread_config.get("aid")
    if value is None:
        return None
    if type(value) is int:
        return value
    raise ValueError(f"帖子配置字段 aid 必须是整数或null：{thread_config!r}")


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
            raise ValueError("帖子配置字段 ThreadList 必须是数组。")

        thread_items = cast(list[object], raw_thread_list)
        self.ThreadList = [_parse_thread_config(item) for item in thread_items]

    def add_thread(
        self,
        thread_name: str,
        tid: int,
        aid: Optional[int] = None,
        description: str = "",
    ) -> bool:
        thread_config: ThreadConfig = {
            "thread_name": thread_name,
            "tid": tid,
        }
        if aid is not None:
            thread_config["aid"] = aid
        if description:
            thread_config["description"] = description
        for existing in self.ThreadList:
            if (
                thread_config_tid(existing) == tid
                and thread_config_aid(existing) == aid
            ):
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
            if thread_config_name(thread) == name:
                return thread_config_tid(thread), thread_config_aid(thread)
        print(f"未找到名称为{name}的帖子配置。")
        raise ValueError(f"未找到名称为{name}的帖子配置。")

    if tid is not None:
        return tid, aid

    raise ValueError("name或tid参数必须提供其一以指定要备份的帖子。")
