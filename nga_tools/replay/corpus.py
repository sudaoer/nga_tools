from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, TypeAlias, cast

from nga_tools.backup.floor_models import ORIGINAL_POSTS_PER_PAGE
from nga_tools.core.hashing import hash_text
from nga_tools.core.sqlite import configure_readonly_connection

_CORPUS_FORMAT_VERSION = 7

PageKey: TypeAlias = tuple[int, Optional[int], int]
ProgressCallback: TypeAlias = Callable[[int, int, str], None]


class _Hasher(Protocol):
    def update(self, data: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


class ReplayCorpusError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReplayPage:
    payload: bytes
    floor_map_original: bool


@dataclass(frozen=True, slots=True)
class ReplayPidTarget:
    tid: int
    page_number: int


@dataclass(frozen=True, slots=True)
class ImageReplayEntry:
    path: Path
    size: int
    mtime_ns: int

    def is_current(self) -> bool:
        try:
            stat_result = self.path.stat()
        except OSError:
            return False
        return (
            self.path.is_file()
            and stat_result.st_size == self.size
            and stat_result.st_mtime_ns == self.mtime_ns
        )


@dataclass(frozen=True, slots=True)
class _ReplayPost:
    pid: int
    lou: int
    content: str
    author_name: str | None
    author_uid: int | None
    postdate: int | str | None

    def response_post(
        self,
        *,
        lou: int | None = None,
        pid: int | None = None,
    ) -> dict[str, object]:
        author: dict[str, object] = {}
        if self.author_uid is not None:
            author["uid"] = self.author_uid
        if self.author_name is not None:
            author["username"] = self.author_name
        post: dict[str, object] = {
            "pid": self.pid if pid is None else pid,
            "lou": self.lou if lou is None else lou,
            "content": self.content,
            "author": author,
            "attches": [],
        }
        if self.postdate is not None:
            post["postdate"] = self.postdate
        return post


@dataclass(frozen=True, slots=True)
class _FloorMapEntry:
    author_lou: int
    pid: int | None
    original_lou: int | None
    original_pid: int | None


@dataclass(frozen=True, slots=True)
class _FloorMapData:
    entries_by_author_lou: dict[int, _FloorMapEntry]
    candidates_by_author_lou: dict[int, tuple[int, ...]]

    @property
    def author_row_count(self) -> int:
        if not self.entries_by_author_lou:
            return 0
        return max(self.entries_by_author_lou) + 1


@dataclass(frozen=True, slots=True)
class _FloorMapOriginalThread:
    tid: int
    source_aid: int
    row_count: int
    posts_by_original_lou: dict[int, _ReplayPost]
    omitted_original_lous: frozenset[int]

    @property
    def page_count(self) -> int:
        return max(
            1,
            (self.row_count + ORIGINAL_POSTS_PER_PAGE - 1)
            // ORIGINAL_POSTS_PER_PAGE,
        )

    def page(self, page_number: int) -> ReplayPage | None:
        if page_number < 1 or page_number > self.page_count:
            return None

        start_lou = (page_number - 1) * ORIGINAL_POSTS_PER_PAGE
        end_lou = min(start_lou + ORIGINAL_POSTS_PER_PAGE, self.row_count)
        posts: list[dict[str, object]] = []
        for original_lou in range(start_lou, end_lou):
            if original_lou in self.omitted_original_lous:
                continue
            known_post = self.posts_by_original_lou.get(original_lou)
            if known_post is None:
                posts.append(
                    {
                        "pid": -(original_lou + 1),
                        "lou": original_lou,
                        "content": "",
                        "author": {"uid": 0, "username": "replay-filler"},
                        "attches": [],
                    }
                )
                continue
            posts.append(known_post.response_post(lou=original_lou))

        payload = json.dumps(
            {
                "code": 0,
                "currentPage": page_number,
                "totalPage": self.page_count,
                "vrows": self.row_count,
                "result": posts,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return ReplayPage(payload=payload, floor_map_original=True)


@dataclass(frozen=True, slots=True)
class ReplayManifest:
    corpus_id: str
    source_output: str
    thread_config: str
    thread_count: int
    archive_content_post_count: int
    archive_content_page_count: int
    archive_content_page_payload_bytes: int
    floor_map_original_thread_count: int
    floor_map_original_page_count: int
    locatable_pid_count: int
    image_mapping_count: int
    available_image_mapping_count: int
    unavailable_image_mapping_count: int
    unique_image_file_count: int
    unique_image_file_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "corpus_id": self.corpus_id,
            "corpus_format_version": _CORPUS_FORMAT_VERSION,
            "source_output": self.source_output,
            "thread_config": self.thread_config,
            "thread_count": self.thread_count,
            "archive_content_post_count": self.archive_content_post_count,
            "archive_content_page_count": self.archive_content_page_count,
            "archive_content_page_payload_bytes": (
                self.archive_content_page_payload_bytes
            ),
            "floor_map_original_thread_count": (
                self.floor_map_original_thread_count
            ),
            "floor_map_original_page_count": self.floor_map_original_page_count,
            "locatable_pid_count": self.locatable_pid_count,
            "image_mapping_count": self.image_mapping_count,
            "available_image_mapping_count": self.available_image_mapping_count,
            "unavailable_image_mapping_count": self.unavailable_image_mapping_count,
            "unique_image_file_count": self.unique_image_file_count,
            "unique_image_file_bytes": self.unique_image_file_bytes,
        }


@dataclass(frozen=True, slots=True)
class ReplayCorpus:
    content_pages: dict[PageKey, bytes]
    floor_map_original_threads: dict[int, _FloorMapOriginalThread]
    pid_targets: dict[int, ReplayPidTarget]
    images_by_url: dict[str, ImageReplayEntry]
    manifest: ReplayManifest

    def page(self, tid: int, aid: Optional[int], page_number: int) -> ReplayPage | None:
        content_payload = self.content_pages.get((tid, aid, page_number))
        if content_payload is not None:
            return ReplayPage(payload=content_payload, floor_map_original=False)
        if aid is not None:
            return None
        original_thread = self.floor_map_original_threads.get(tid)
        if original_thread is None:
            return None
        return original_thread.page(page_number)

    def image(self, url: str) -> ImageReplayEntry | None:
        entry = self.images_by_url.get(_normalize_image_url(url))
        if entry is None or not entry.is_current():
            return None
        return entry

    def pid_target(self, pid: int) -> ReplayPidTarget | None:
        return self.pid_targets.get(pid)


@dataclass(frozen=True, slots=True)
class _ThreadReplayConfig:
    tid: int
    aid: Optional[int]
    replies: Optional[int]


@dataclass(frozen=True, slots=True)
class _FileState:
    exists: bool
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _DatabaseState:
    database: _FileState
    wal: _FileState


@dataclass(frozen=True, slots=True)
class _ArchiveContent:
    posts_by_lou: dict[int, _ReplayPost]
    database_state: _DatabaseState


def _file_state(path: Path) -> _FileState:
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return _FileState(False, 0, 0)
    return _FileState(True, stat_result.st_size, stat_result.st_mtime_ns)


def _database_state(path: Path) -> _DatabaseState:
    return _DatabaseState(
        database=_file_state(path),
        wal=_file_state(Path(f"{path}-wal")),
    )


def _connect_frozen(
    path: Path,
    *,
    expected_state: _DatabaseState | None = None,
) -> tuple[sqlite3.Connection, _DatabaseState]:
    state = _database_state(path)
    if not state.database.exists:
        raise ReplayCorpusError(f"重放源数据库不存在：{path}")
    if expected_state is not None and state != expected_state:
        raise ReplayCorpusError(f"读取期间重放源数据库发生变化：{path}")
    if state.wal.exists and state.wal.size != 0:
        raise ReplayCorpusError(
            f"重放源数据库存在未检查点的WAL，不能冻结：{path}。"
            "请停止写入进程并完成检查点后重试。"
        )
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    configure_readonly_connection(connection)
    connection.execute("PRAGMA query_only = ON")
    return connection, state


def _verify_frozen(path: Path, expected_state: _DatabaseState) -> None:
    if _database_state(path) != expected_state:
        raise ReplayCorpusError(f"读取期间重放源数据库发生变化：{path}")


def _hash_fields(hasher: _Hasher, *values: object) -> None:
    for value in values:
        encoded = str(value).encode("utf-8")
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)


def _read_thread_configs(path: Path, hasher: _Hasher) -> list[_ThreadReplayConfig]:
    before = _file_state(path)
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError as error:
        raise ReplayCorpusError(f"重放线程配置不存在：{path}") from error
    after = _file_state(path)
    if before != after:
        raise ReplayCorpusError(f"读取期间线程配置发生变化：{path}")
    hasher.update(raw_bytes)

    try:
        raw_data: object = json.loads(raw_bytes)
    except json.JSONDecodeError as error:
        raise ReplayCorpusError(f"重放线程配置不是有效JSON：{path}") from error
    if not isinstance(raw_data, dict):
        raise ReplayCorpusError(f"重放线程配置顶层必须是对象：{path}")
    raw_thread_list = cast(dict[str, object], raw_data).get("ThreadList")
    if not isinstance(raw_thread_list, list):
        raise ReplayCorpusError(f"重放线程配置缺少ThreadList数组：{path}")

    configs: list[_ThreadReplayConfig] = []
    seen_tids: set[int] = set()
    for raw_item in cast(list[object], raw_thread_list):
        if not isinstance(raw_item, dict):
            raise ReplayCorpusError(f"重放线程配置项不是对象：{raw_item!r}")
        item = cast(dict[str, object], raw_item)
        tid = item.get("tid")
        aid = item.get("aid")
        replies = item.get("replies")
        if type(tid) is not int or tid <= 0:
            raise ReplayCorpusError(f"重放线程配置tid无效：{raw_item!r}")
        if aid is not None and (type(aid) is not int or aid <= 0):
            raise ReplayCorpusError(f"重放线程配置aid无效：{raw_item!r}")
        if replies is not None and (type(replies) is not int or replies < 0):
            raise ReplayCorpusError(f"重放线程配置replies无效：{raw_item!r}")
        if tid in seen_tids:
            raise ReplayCorpusError(f"重放线程配置包含重复tid：{tid}")
        seen_tids.add(tid)
        configs.append(_ThreadReplayConfig(tid=tid, aid=aid, replies=replies))
    if not configs:
        raise ReplayCorpusError(f"重放线程配置中没有帖子：{path}")
    return configs


def _postdate_from_json(value: object, *, source: str) -> int | str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReplayCorpusError(f"{source} postdate_json无效。")
    try:
        raw_postdate: object = json.loads(value)
    except json.JSONDecodeError as error:
        raise ReplayCorpusError(f"{source} postdate_json不是有效JSON。") from error
    if type(raw_postdate) is int:
        return raw_postdate
    if isinstance(raw_postdate, str) and raw_postdate.strip():
        return raw_postdate.strip()
    raise ReplayCorpusError(f"{source} postdate_json内容无效。")


def _read_archive_content(path: Path) -> _ArchiveContent:
    try:
        connection, database_state = _connect_frozen(path)
        with closing(connection):
            rows = cast(
                list[tuple[object, ...]],
                connection.execute(
                    """
                    WITH latest AS (
                        SELECT
                            id,
                            lou,
                            pid,
                            content,
                            source_hash,
                            ROW_NUMBER() OVER (
                                PARTITION BY lou
                                ORDER BY last_seen_at DESC, id DESC
                            ) AS row_number
                        FROM post_versions
                    )
                    SELECT
                        latest.lou,
                        latest.pid,
                        latest.content,
                        latest.source_hash,
                        metadata.pid,
                        metadata.author_name,
                        metadata.author_uid,
                        metadata.postdate_json
                    FROM latest
                    LEFT JOIN post_latest_metadata AS metadata
                        ON metadata.pid = latest.pid
                        AND metadata.lou = latest.lou
                    WHERE latest.row_number = 1
                    ORDER BY latest.lou
                    """
                ).fetchall(),
            )
        _verify_frozen(path, database_state)
    except sqlite3.Error as error:
        raise ReplayCorpusError(f"无法读取重放归档内容：{path}: {error}") from error

    posts_by_lou: dict[int, _ReplayPost] = {}
    for row in rows:
        if len(row) != 8:
            raise ReplayCorpusError(f"重放归档内容行字段数无效：{path}")
        (
            raw_lou,
            raw_pid,
            raw_content,
            raw_source_hash,
            raw_metadata_pid,
            raw_author_name,
            raw_author_uid,
            raw_postdate_json,
        ) = row
        if type(raw_lou) is not int or raw_lou < 0:
            raise ReplayCorpusError(f"重放归档内容lou无效：{path}: {raw_lou!r}")
        if type(raw_pid) is not int or raw_pid < 0:
            raise ReplayCorpusError(f"重放归档内容pid无效：{path}: {raw_pid!r}")
        if not isinstance(raw_content, str) or not isinstance(raw_source_hash, str):
            raise ReplayCorpusError(f"重放归档正文或source_hash无效：{path}")
        if hash_text(raw_content) != raw_source_hash:
            raise ReplayCorpusError(
                f"重放归档第{raw_lou}楼source_hash与正文不匹配：{path}"
            )
        if raw_metadata_pid != raw_pid:
            raise ReplayCorpusError(f"重放归档第{raw_lou}楼缺少最新元数据：{path}")
        if raw_author_name is not None and not isinstance(raw_author_name, str):
            raise ReplayCorpusError(f"重放归档第{raw_lou}楼作者名无效：{path}")
        if raw_author_uid is not None and type(raw_author_uid) is not int:
            raise ReplayCorpusError(f"重放归档第{raw_lou}楼作者UID无效：{path}")
        source = f"{path}第{raw_lou}楼"
        post = _ReplayPost(
            pid=raw_pid,
            lou=raw_lou,
            content=raw_content,
            author_name=raw_author_name,
            author_uid=raw_author_uid,
            postdate=_postdate_from_json(raw_postdate_json, source=source),
        )
        if raw_lou in posts_by_lou:
            raise ReplayCorpusError(f"重放归档包含重复有效楼层{raw_lou}：{path}")
        posts_by_lou[raw_lou] = post
    return _ArchiveContent(
        posts_by_lou=posts_by_lou,
        database_state=database_state,
    )


def _page_count(row_count: int) -> int:
    return max(
        1,
        (row_count + ORIGINAL_POSTS_PER_PAGE - 1) // ORIGINAL_POSTS_PER_PAGE,
    )


def _content_page_payloads(
    posts_by_lou: dict[int, _ReplayPost],
    *,
    row_count: int,
) -> dict[int, bytes]:
    total_pages = _page_count(row_count)
    payloads: dict[int, bytes] = {}
    for page_number in range(1, total_pages + 1):
        start_lou = (page_number - 1) * ORIGINAL_POSTS_PER_PAGE
        end_lou = min(start_lou + ORIGINAL_POSTS_PER_PAGE, row_count)
        posts = [
            post.response_post()
            for lou in range(start_lou, end_lou)
            if (post := posts_by_lou.get(lou)) is not None
        ]
        payloads[page_number] = json.dumps(
            {
                "code": 0,
                "currentPage": page_number,
                "totalPage": total_pages,
                "vrows": row_count,
                "result": posts,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    return payloads


def _optional_int(value: object, *, source: str) -> Optional[int]:
    if value is None:
        return None
    if type(value) is int:
        return value
    raise ReplayCorpusError(f"{source}不是整数或null：{value!r}")


def _read_floor_map(
    path: Path,
    config: _ThreadReplayConfig,
    hasher: _Hasher,
    *,
    expected_database_state: _DatabaseState,
    required: bool,
) -> _FloorMapData | None:
    if config.aid is None:
        raise ReplayCorpusError("原帖归档不需要作者楼层映射。")
    try:
        connection, database_state = _connect_frozen(
            path,
            expected_state=expected_database_state,
        )
        with closing(connection):
            state_row = connection.execute(
                "SELECT tid, aid FROM floor_map_state WHERE singleton = 1"
            ).fetchone()
            if state_row is None:
                if required:
                    raise ReplayCorpusError(
                        f"重放源归档缺少匹配的楼层映射：{path}"
                    )
                entry_rows: list[tuple[object, object, object, object]] = []
                candidate_rows: list[tuple[object, object, object]] = []
            elif state_row != (config.tid, config.aid):
                raise ReplayCorpusError(f"重放源归档缺少匹配的楼层映射：{path}")
            else:
                entry_rows = cast(
                    list[tuple[object, object, object, object]],
                    connection.execute(
                        """
                        SELECT author_lou, pid, original_lou, original_pid
                        FROM floor_map_entries
                        ORDER BY author_lou
                        """
                    ).fetchall(),
                )
                candidate_rows = cast(
                    list[tuple[object, object, object]],
                    connection.execute(
                        """
                        SELECT author_lou, candidate_index, original_lou
                        FROM floor_map_candidates
                        ORDER BY author_lou, candidate_index
                        """
                    ).fetchall(),
                )
        _verify_frozen(path, database_state)
    except sqlite3.Error as error:
        raise ReplayCorpusError(f"无法读取重放楼层映射：{path}: {error}") from error

    if state_row is None:
        _hash_fields(hasher, "floor_map_missing", config.tid, config.aid)
        return None

    entries_by_author_lou: dict[int, _FloorMapEntry] = {}
    for raw_author_lou, raw_pid, raw_original_lou, raw_original_pid in entry_rows:
        if type(raw_author_lou) is not int or raw_author_lou < 0:
            raise ReplayCorpusError(f"重放源楼层映射author_lou无效：{path}")
        pid = _optional_int(raw_pid, source=f"{path} pid")
        original_lou = _optional_int(
            raw_original_lou,
            source=f"{path} original_lou",
        )
        original_pid = _optional_int(
            raw_original_pid,
            source=f"{path} original_pid",
        )
        if pid is not None and pid < 0:
            raise ReplayCorpusError(f"重放源楼层映射pid无效：{path}")
        if original_lou is not None and original_lou < 0:
            raise ReplayCorpusError(f"重放源楼层映射original_lou无效：{path}")
        if original_pid is not None and original_pid <= 0:
            raise ReplayCorpusError(f"重放源楼层映射original_pid无效：{path}")
        if original_pid is not None and (pid is not None or original_lou is None):
            raise ReplayCorpusError(f"重放源楼层映射恢复字段组合无效：{path}")
        entry = _FloorMapEntry(
            author_lou=raw_author_lou,
            pid=pid,
            original_lou=original_lou,
            original_pid=original_pid,
        )
        if raw_author_lou in entries_by_author_lou:
            raise ReplayCorpusError(
                f"重放源楼层映射包含重复author_lou={raw_author_lou}：{path}"
            )
        entries_by_author_lou[raw_author_lou] = entry
        _hash_fields(
            hasher,
            "floor",
            config.tid,
            config.aid,
            raw_author_lou,
            pid,
            original_lou,
            original_pid,
        )

    candidate_lists: dict[int, list[int]] = {}
    for raw_author_lou, raw_candidate_index, raw_original_lou in candidate_rows:
        if (
            type(raw_author_lou) is not int
            or type(raw_candidate_index) is not int
            or raw_candidate_index < 0
            or type(raw_original_lou) is not int
            or raw_original_lou < 0
        ):
            raise ReplayCorpusError(f"重放源楼层候选行无效：{path}")
        entry = entries_by_author_lou.get(raw_author_lou)
        if entry is None or entry.pid is not None or entry.original_lou is not None:
            raise ReplayCorpusError(f"重放源楼层候选没有匹配的缺失楼：{path}")
        candidates = candidate_lists.setdefault(raw_author_lou, [])
        if raw_candidate_index != len(candidates):
            raise ReplayCorpusError(f"重放源楼层候选序号不连续：{path}")
        candidates.append(raw_original_lou)
        _hash_fields(
            hasher,
            "candidate",
            config.tid,
            config.aid,
            raw_author_lou,
            raw_candidate_index,
            raw_original_lou,
        )

    return _FloorMapData(
        entries_by_author_lou=entries_by_author_lou,
        candidates_by_author_lou={
            author_lou: tuple(candidates)
            for author_lou, candidates in candidate_lists.items()
        },
    )


def _pid_targets_from_floor_map(
    config: _ThreadReplayConfig,
    floor_map: _FloorMapData,
    hasher: _Hasher,
) -> dict[int, ReplayPidTarget]:
    targets: dict[int, ReplayPidTarget] = {}
    for entry in floor_map.entries_by_author_lou.values():
        if entry.original_lou is None:
            continue
        target = ReplayPidTarget(
            tid=config.tid,
            page_number=entry.original_lou // ORIGINAL_POSTS_PER_PAGE + 1,
        )
        for pid in (entry.pid, entry.original_pid):
            if pid is None or pid <= 0:
                continue
            existing = targets.get(pid)
            if existing is not None and existing != target:
                raise ReplayCorpusError(
                    f"重放语料PID {pid} 映射到多个目标：{existing}、{target}"
                )
            targets[pid] = target

    for pid, target in sorted(targets.items()):
        _hash_fields(
            hasher,
            "pid_target",
            pid,
            target.tid,
            target.page_number,
        )
    return targets


def _validate_original_archive_content(
    path: Path,
    original_content: _ArchiveContent,
    author_content: _ArchiveContent,
    floor_map: _FloorMapData,
) -> None:
    for entry in floor_map.entries_by_author_lou.values():
        if entry.original_lou is None:
            continue
        expected_pid = entry.pid if entry.pid is not None else entry.original_pid
        if expected_pid is None:
            continue
        original_post = original_content.posts_by_lou.get(entry.original_lou)
        if original_post is None or original_post.pid != expected_pid:
            raise ReplayCorpusError(
                "原帖内容与作者楼层映射不匹配："
                f"{path}: original_lou={entry.original_lou}, pid={expected_pid}"
            )
        if entry.original_pid is None:
            continue
        recovered_post = author_content.posts_by_lou.get(entry.author_lou)
        if (
            recovered_post is None
            or recovered_post.response_post(lou=entry.original_lou)
            != original_post.response_post()
        ):
            raise ReplayCorpusError(
                "原帖内容与作者归档恢复楼不一致："
                f"{path}: original_lou={entry.original_lou}"
            )


def _build_floor_map_original_thread(
    path: Path,
    config: _ThreadReplayConfig,
    content: _ArchiveContent,
    floor_map: _FloorMapData,
    hasher: _Hasher,
) -> _FloorMapOriginalThread:
    if config.aid is None:
        raise ReplayCorpusError("原帖归档不需要楼层映射合成响应。")

    posts_by_original_lou: dict[int, _ReplayPost] = {}
    omitted_original_lous: set[int] = set()
    max_original_lou = -1
    for entry in floor_map.entries_by_author_lou.values():
        post = content.posts_by_lou.get(entry.author_lou)
        if entry.pid is not None:
            if entry.original_lou is None:
                raise ReplayCorpusError(
                    f"重放源存在未映射的作者帖子，无法合成原帖：{path}"
                )
            if post is None or post.pid != entry.pid or post.author_uid == -1:
                raise ReplayCorpusError(
                    f"重放源作者楼内容与楼层映射不匹配：{path} "
                    f"author_lou={entry.author_lou}"
                )
            replay_post = post
        elif entry.original_lou is None:
            continue
        elif entry.original_pid is None:
            omitted_original_lous.add(entry.original_lou)
            max_original_lou = max(max_original_lou, entry.original_lou)
            continue
        else:
            if (
                post is None
                or post.pid != entry.original_pid
                or post.author_uid != -1
            ):
                raise ReplayCorpusError(
                    f"重放源恢复楼内容与楼层映射不匹配：{path} "
                    f"author_lou={entry.author_lou}"
                )
            replay_post = post

        original_lou = entry.original_lou
        existing = posts_by_original_lou.get(original_lou)
        if existing is not None and existing != replay_post:
            raise ReplayCorpusError(
                f"多个内容楼映射到同一原楼层{original_lou}：{path}"
            )
        posts_by_original_lou[original_lou] = replay_post
        max_original_lou = max(max_original_lou, original_lou)

    for author_lou, candidates in floor_map.candidates_by_author_lou.items():
        for original_lou in candidates:
            if original_lou in posts_by_original_lou:
                raise ReplayCorpusError(
                    "缺失楼候选原楼层与已知内容冲突："
                    f"{path}: author_lou={author_lou}, original_lou={original_lou}"
                )
            omitted_original_lous.add(original_lou)
            max_original_lou = max(max_original_lou, original_lou)

    for author_lou, post in content.posts_by_lou.items():
        entry = floor_map.entries_by_author_lou.get(author_lou)
        if entry is None:
            expected_pid = None
        elif post.author_uid == -1:
            expected_pid = entry.original_pid
        else:
            expected_pid = entry.pid
        if expected_pid != post.pid:
            raise ReplayCorpusError(
                f"重放源第{author_lou}楼内容无法通过楼层映射路由：{path}"
            )

    configured_rows = 0 if config.replies is None else config.replies + 1
    row_count = max(1, configured_rows, max_original_lou + 1)
    _hash_fields(
        hasher,
        "floor_map_original",
        config.tid,
        config.aid,
        row_count,
    )
    for original_lou, post in sorted(posts_by_original_lou.items()):
        response_json = json.dumps(
            post.response_post(lou=original_lou),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        _hash_fields(
            hasher,
            "floor_map_original_post",
            original_lou,
            hash_text(response_json),
        )
    for original_lou in sorted(omitted_original_lous):
        _hash_fields(hasher, "floor_map_original_omitted", original_lou)

    return _FloorMapOriginalThread(
        tid=config.tid,
        source_aid=config.aid,
        row_count=row_count,
        posts_by_original_lou=posts_by_original_lou,
        omitted_original_lous=frozenset(omitted_original_lous),
    )


def _normalize_image_url(url: str) -> str:
    return url.replace(",", "")


@dataclass(frozen=True, slots=True)
class _ImageLoadResult:
    images_by_url: dict[str, ImageReplayEntry]
    mapping_count: int
    unavailable_mapping_count: int
    unique_file_count: int
    unique_file_bytes: int


def _load_images(source_output: Path, hasher: _Hasher) -> _ImageLoadResult:
    index_path = source_output / "image_index.sqlite3"
    images_root = (source_output / "images_unique").resolve()
    try:
        connection, database_state = _connect_frozen(index_path)
        with closing(connection):
            rows = cast(
                list[tuple[object, object]],
                connection.execute(
                    """
                    SELECT url, unique_rel_path
                    FROM image_mappings
                    ORDER BY url
                    """
                ).fetchall(),
            )
        _verify_frozen(index_path, database_state)
    except sqlite3.Error as error:
        raise ReplayCorpusError(f"无法读取重放图片索引：{index_path}: {error}") from error

    images_by_url: dict[str, ImageReplayEntry] = {}
    entries_by_relative_path: dict[str, ImageReplayEntry | None] = {}
    unavailable_mapping_count = 0
    unique_file_bytes = 0
    for raw_url, raw_relative_path in rows:
        if not isinstance(raw_url, str) or not isinstance(raw_relative_path, str):
            raise ReplayCorpusError(f"重放图片索引行无效：{index_path}")
        relative_path = Path(raw_relative_path)
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or relative_path.parts[0] != "images_unique"
        ):
            raise ReplayCorpusError(f"重放图片索引路径越界：{raw_relative_path}")
        resolved_path = (source_output / relative_path).resolve()
        if not resolved_path.is_relative_to(images_root):
            raise ReplayCorpusError(f"重放图片索引路径越界：{raw_relative_path}")

        entry = entries_by_relative_path.get(raw_relative_path)
        if raw_relative_path not in entries_by_relative_path:
            try:
                stat_result = resolved_path.stat()
            except OSError:
                entry = None
            else:
                entry = (
                    ImageReplayEntry(
                        path=resolved_path,
                        size=stat_result.st_size,
                        mtime_ns=stat_result.st_mtime_ns,
                    )
                    if resolved_path.is_file()
                    else None
                )
            entries_by_relative_path[raw_relative_path] = entry
            if entry is not None:
                unique_file_bytes += entry.size

        normalized_url = _normalize_image_url(raw_url)
        existing_entry = images_by_url.get(normalized_url)
        if entry is None:
            unavailable_mapping_count += 1
        elif existing_entry is not None and existing_entry != entry:
            raise ReplayCorpusError(f"规范化图片URL映射冲突：{normalized_url}")
        else:
            images_by_url[normalized_url] = entry
        _hash_fields(
            hasher,
            "image",
            normalized_url,
            raw_relative_path,
            "missing" if entry is None else entry.size,
            "missing" if entry is None else entry.mtime_ns,
        )

    return _ImageLoadResult(
        images_by_url=images_by_url,
        mapping_count=len(rows),
        unavailable_mapping_count=unavailable_mapping_count,
        unique_file_count=sum(
            entry is not None for entry in entries_by_relative_path.values()
        ),
        unique_file_bytes=unique_file_bytes,
    )


def _archive_path(source_output: Path, tid: int, aid: Optional[int]) -> Path:
    aid_key = str(aid) if aid is not None else "all"
    return source_output / f"{tid}_{aid_key}" / "archive.sqlite3"


def load_replay_corpus(
    source_output: Path,
    thread_config_path: Path,
    *,
    on_progress: ProgressCallback | None = None,
) -> ReplayCorpus:
    resolved_output = source_output.resolve()
    resolved_thread_config = thread_config_path.resolve()
    if not resolved_output.is_dir():
        raise ReplayCorpusError(f"重放源输出目录不存在：{resolved_output}")

    hasher = hashlib.sha256()
    _hash_fields(hasher, "corpus_format", _CORPUS_FORMAT_VERSION)
    configs = _read_thread_configs(resolved_thread_config, hasher)
    content_pages: dict[PageKey, bytes] = {}
    floor_map_original_threads: dict[int, _FloorMapOriginalThread] = {}
    pid_targets: dict[int, ReplayPidTarget] = {}
    archive_content_post_count = 0
    archive_content_page_payload_bytes = 0
    total_configs = len(configs)
    total_work_items = total_configs + 1

    def register_content_pages(
        *,
        tid: int,
        aid: int | None,
        posts_by_lou: dict[int, _ReplayPost],
        row_count: int,
    ) -> None:
        nonlocal archive_content_page_payload_bytes
        page_payloads = _content_page_payloads(
            posts_by_lou,
            row_count=row_count,
        )
        for page_number, payload in page_payloads.items():
            key = (tid, aid, page_number)
            if key in content_pages:
                raise ReplayCorpusError(f"重放语料包含重复分页：{key}")
            content_pages[key] = payload
            archive_content_page_payload_bytes += len(payload)
            _hash_fields(
                hasher,
                "archive_content_page",
                tid,
                aid,
                page_number,
                hashlib.sha256(payload).hexdigest(),
            )

    for index, config in enumerate(configs, start=1):
        if on_progress is not None:
            on_progress(index - 1, total_work_items, f"读取 tid={config.tid}")
        configured_archive = _archive_path(
            resolved_output,
            config.tid,
            config.aid,
        )
        configured_content = _read_archive_content(configured_archive)
        archive_content_post_count += len(configured_content.posts_by_lou)
        if config.aid is None:
            configured_rows = 0 if config.replies is None else config.replies + 1
            content_rows = (
                0
                if not configured_content.posts_by_lou
                else max(configured_content.posts_by_lou) + 1
            )
            register_content_pages(
                tid=config.tid,
                aid=None,
                posts_by_lou=configured_content.posts_by_lou,
                row_count=max(configured_rows, content_rows),
            )
            continue

        original_archive = _archive_path(resolved_output, config.tid, None)
        has_original_archive = original_archive.is_file()
        floor_map = _read_floor_map(
            configured_archive,
            config,
            hasher,
            expected_database_state=configured_content.database_state,
            required=not has_original_archive,
        )
        archive_pid_targets = (
            {}
            if floor_map is None
            else _pid_targets_from_floor_map(config, floor_map, hasher)
        )
        for pid, target in archive_pid_targets.items():
            existing = pid_targets.get(pid)
            if existing is not None and existing != target:
                raise ReplayCorpusError(
                    f"重放语料PID {pid} 映射到多个目标：{existing}、{target}"
                )
            pid_targets[pid] = target

        author_posts = {
            lou: post
            for lou, post in configured_content.posts_by_lou.items()
            if post.author_uid != -1
        }
        author_content_rows = 0 if not author_posts else max(author_posts) + 1
        register_content_pages(
            tid=config.tid,
            aid=config.aid,
            posts_by_lou=author_posts,
            row_count=max(
                0 if floor_map is None else floor_map.author_row_count,
                author_content_rows,
            ),
        )

        if has_original_archive:
            original_content = _read_archive_content(original_archive)
            archive_content_post_count += len(original_content.posts_by_lou)
            if floor_map is not None:
                _validate_original_archive_content(
                    original_archive,
                    original_content,
                    configured_content,
                    floor_map,
                )
            configured_rows = 0 if config.replies is None else config.replies + 1
            original_content_rows = (
                0
                if not original_content.posts_by_lou
                else max(original_content.posts_by_lou) + 1
            )
            register_content_pages(
                tid=config.tid,
                aid=None,
                posts_by_lou=original_content.posts_by_lou,
                row_count=max(configured_rows, original_content_rows),
            )
        else:
            if floor_map is None:
                raise ReplayCorpusError(
                    f"缺少原帖归档和楼层映射，无法合成原帖：{configured_archive}"
                )
            floor_map_original_threads[config.tid] = (
                _build_floor_map_original_thread(
                    configured_archive,
                    config,
                    configured_content,
                    floor_map,
                    hasher,
                )
            )

    if on_progress is not None:
        on_progress(total_configs, total_work_items, "读取图片索引")
    image_result = _load_images(resolved_output, hasher)
    if on_progress is not None:
        on_progress(total_work_items, total_work_items, "重放语料读取完成")
    manifest = ReplayManifest(
        corpus_id=hasher.hexdigest(),
        source_output=str(resolved_output),
        thread_config=str(resolved_thread_config),
        thread_count=total_configs,
        archive_content_post_count=archive_content_post_count,
        archive_content_page_count=len(content_pages),
        archive_content_page_payload_bytes=archive_content_page_payload_bytes,
        floor_map_original_thread_count=len(floor_map_original_threads),
        floor_map_original_page_count=sum(
            thread.page_count for thread in floor_map_original_threads.values()
        ),
        locatable_pid_count=len(pid_targets),
        image_mapping_count=image_result.mapping_count,
        available_image_mapping_count=len(image_result.images_by_url),
        unavailable_image_mapping_count=image_result.unavailable_mapping_count,
        unique_image_file_count=image_result.unique_file_count,
        unique_image_file_bytes=image_result.unique_file_bytes,
    )
    return ReplayCorpus(
        content_pages=content_pages,
        floor_map_original_threads=floor_map_original_threads,
        pid_targets=pid_targets,
        images_by_url=image_result.images_by_url,
        manifest=manifest,
    )
