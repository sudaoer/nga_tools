from __future__ import annotations

from collections.abc import Callable
from typing import Optional, TypeAlias

CommandArgs: TypeAlias = dict[str, object]
CommandHandler: TypeAlias = Callable[[CommandArgs], None]


def required_str(args: CommandArgs, key: str) -> str:
    value = args.get(key)
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"缺少字符串参数：--{key}")


def optional_str(args: CommandArgs, key: str) -> Optional[str]:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ValueError(f"--{key}必须是字符串。")


def required_int(args: CommandArgs, key: str) -> int:
    value = args.get(key)
    if type(value) is int:
        return value
    raise ValueError(f"缺少整数参数：--{key}")


def optional_int(args: CommandArgs, key: str) -> Optional[int]:
    value = args.get(key)
    if value is None:
        return None
    if type(value) is int:
        return value
    raise ValueError(f"--{key}必须是整数。")


def optional_float(args: CommandArgs, key: str) -> Optional[float]:
    value = args.get(key)
    if value is None:
        return None
    if type(value) is float or type(value) is int:
        return float(value)
    raise ValueError(f"--{key}必须是浮点数。")


def optional_bool(args: CommandArgs, key: str) -> bool:
    value = args.get(key)
    if value is None:
        return False
    if type(value) is bool:
        return value
    raise ValueError(f"--{key}必须是布尔值。")
