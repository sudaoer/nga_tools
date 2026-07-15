from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeAlias, cast

JsonObject: TypeAlias = dict[str, object]


class ReplayProfileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TrafficProfile:
    latency_ms: int
    bandwidth_bytes_per_second: int
    max_inflight: int


@dataclass(frozen=True, slots=True)
class ReplayProfile:
    api: TrafficProfile
    image: TrafficProfile
    chunk_bytes: int
    audio: TrafficProfile | None = None

    @property
    def effective_audio(self) -> TrafficProfile:
        return self.image if self.audio is None else self.audio

    def as_dict(self) -> dict[str, object]:
        return {
            "api": asdict(self.api),
            "image": asdict(self.image),
            "audio": asdict(self.effective_audio),
            "chunk_bytes": self.chunk_bytes,
        }

    @property
    def profile_id(self) -> str:
        payload = json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _read_json_object(path: Path) -> JsonObject:
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReplayProfileError(f"重放限速配置不存在：{path}") from error
    except json.JSONDecodeError as error:
        raise ReplayProfileError(f"重放限速配置不是有效JSON：{path}") from error

    if not isinstance(raw_data, dict):
        raise ReplayProfileError(f"重放限速配置顶层必须是对象：{path}")
    data = cast(dict[object, object], raw_data)
    if not all(isinstance(key, str) for key in data):
        raise ReplayProfileError(f"重放限速配置键必须是字符串：{path}")
    return cast(JsonObject, data)


def _required_int(
    data: JsonObject,
    key: str,
    *,
    source: str,
    minimum: int,
) -> int:
    value = data.get(key)
    if type(value) is not int or value < minimum:
        requirement = "非负整数" if minimum == 0 else f"不小于{minimum}的整数"
        raise ReplayProfileError(f"{source}.{key}必须是{requirement}。")
    return value


def _required_object(data: JsonObject, key: str, *, source: str) -> JsonObject:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ReplayProfileError(f"{source}.{key}必须是对象。")
    object_value = cast(dict[object, object], value)
    if not all(isinstance(item_key, str) for item_key in object_value):
        raise ReplayProfileError(f"{source}.{key}的键必须是字符串。")
    return cast(JsonObject, object_value)


def _reject_extra_keys(data: JsonObject, allowed: set[str], *, source: str) -> None:
    extra_keys = sorted(set(data) - allowed)
    if extra_keys:
        raise ReplayProfileError(
            f"{source}包含未知配置项：{', '.join(extra_keys)}。"
        )


def _parse_traffic_profile(data: JsonObject, *, source: str) -> TrafficProfile:
    _reject_extra_keys(
        data,
        {"latency_ms", "bandwidth_bytes_per_second", "max_inflight"},
        source=source,
    )
    return TrafficProfile(
        latency_ms=_required_int(data, "latency_ms", source=source, minimum=0),
        bandwidth_bytes_per_second=_required_int(
            data,
            "bandwidth_bytes_per_second",
            source=source,
            minimum=0,
        ),
        max_inflight=_required_int(
            data,
            "max_inflight",
            source=source,
            minimum=1,
        ),
    )


def load_replay_profile(path: Path) -> ReplayProfile:
    data = _read_json_object(path)
    source = str(path)
    _reject_extra_keys(
        data,
        {"api", "image", "audio", "chunk_bytes"},
        source=source,
    )
    image = _parse_traffic_profile(
        _required_object(data, "image", source=source),
        source=f"{source}.image",
    )
    raw_audio = data.get("audio")
    audio = (
        image
        if raw_audio is None
        else _parse_traffic_profile(
            _required_object(data, "audio", source=source),
            source=f"{source}.audio",
        )
    )
    return ReplayProfile(
        api=_parse_traffic_profile(
            _required_object(data, "api", source=source),
            source=f"{source}.api",
        ),
        image=image,
        chunk_bytes=_required_int(
            data,
            "chunk_bytes",
            source=source,
            minimum=1,
        ),
        audio=audio,
    )
