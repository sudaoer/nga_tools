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
from nga_tools.core.sqlite import configure_readonly_connection

_CORPUS_FORMAT_VERSION = 3

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
    synthetic_original: bool


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
class _SyntheticPost:
    pid: int
    author_uid: int
    content: str


@dataclass(frozen=True, slots=True)
class _SyntheticThread:
    tid: int
    source_aid: int
    row_count: int
    posts_by_original_lou: dict[int, _SyntheticPost]
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
                    }
                )
                continue
            posts.append(
                {
                    "pid": known_post.pid,
                    "lou": original_lou,
                    "content": known_post.content,
                    "author": {
                        "uid": known_post.author_uid,
                        "username": (
                            "匿名" if known_post.author_uid == -1 else "replay-author"
                        ),
                    },
                }
            )

        payload = json.dumps(
            {
                "code": 0,
                "currentPage": page_number,
                "totalPage": self.page_count,
                "result": posts,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return ReplayPage(payload=payload, synthetic_original=True)


@dataclass(frozen=True, slots=True)
class ReplayManifest:
    corpus_id: str
    source_output: str
    thread_config: str
    thread_count: int
    exact_page_count: int
    exact_page_payload_bytes: int
    synthetic_thread_count: int
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
            "exact_page_count": self.exact_page_count,
            "exact_page_payload_bytes": self.exact_page_payload_bytes,
            "synthetic_thread_count": self.synthetic_thread_count,
            "image_mapping_count": self.image_mapping_count,
            "available_image_mapping_count": self.available_image_mapping_count,
            "unavailable_image_mapping_count": self.unavailable_image_mapping_count,
            "unique_image_file_count": self.unique_image_file_count,
            "unique_image_file_bytes": self.unique_image_file_bytes,
        }


@dataclass(frozen=True, slots=True)
class ReplayCorpus:
    exact_pages: dict[PageKey, bytes]
    synthetic_threads: dict[int, _SyntheticThread]
    images_by_url: dict[str, ImageReplayEntry]
    manifest: ReplayManifest

    def page(self, tid: int, aid: Optional[int], page_number: int) -> ReplayPage | None:
        exact_payload = self.exact_pages.get((tid, aid, page_number))
        if exact_payload is not None:
            return ReplayPage(payload=exact_payload, synthetic_original=False)
        if aid is not None:
            return None
        synthetic_thread = self.synthetic_threads.get(tid)
        if synthetic_thread is None:
            return None
        return synthetic_thread.page(page_number)

    def image(self, url: str) -> ImageReplayEntry | None:
        entry = self.images_by_url.get(_normalize_image_url(url))
        if entry is None or not entry.is_current():
            return None
        return entry


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
class _LatestArchivePages:
    page_count: int
    pages: dict[int, tuple[str, bytes]]
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


def _page_object(page_json: str, *, source: str) -> dict[str, object]:
    try:
        raw_page: object = json.loads(page_json)
    except json.JSONDecodeError as error:
        raise ReplayCorpusError(f"{source}不是有效JSON。") from error
    if not isinstance(raw_page, dict):
        raise ReplayCorpusError(f"{source}顶层不是对象。")
    page = cast(dict[str, object], raw_page)
    if not isinstance(page.get("result"), list):
        raise ReplayCorpusError(f"{source}缺少result数组。")
    return page


def _read_latest_archive_pages(path: Path) -> _LatestArchivePages:
    try:
        connection, database_state = _connect_frozen(path)
        with closing(connection):
            rows = cast(
                list[tuple[object, object, object]],
                connection.execute(
                    """
                    WITH ranked AS (
                        SELECT
                            page_number,
                            response_hash,
                            page_json,
                            ROW_NUMBER() OVER (
                                PARTITION BY page_number
                                ORDER BY last_seen_at DESC, id DESC
                            ) AS row_number
                        FROM page_snapshots
                    )
                    SELECT page_number, response_hash, page_json
                    FROM ranked
                    WHERE row_number = 1
                    ORDER BY page_number
                    """
                ).fetchall(),
            )
        _verify_frozen(path, database_state)
    except sqlite3.Error as error:
        raise ReplayCorpusError(f"无法读取重放源归档：{path}: {error}") from error

    latest_by_page: dict[int, tuple[str, str]] = {}
    for raw_page_number, raw_response_hash, raw_page_json in rows:
        if (
            type(raw_page_number) is not int
            or raw_page_number < 1
            or not isinstance(raw_response_hash, str)
            or not isinstance(raw_page_json, str)
        ):
            raise ReplayCorpusError(f"重放源归档分页行无效：{path}")
        if (
            hashlib.sha256(raw_page_json.encode("utf-8")).hexdigest()
            != raw_response_hash
        ):
            raise ReplayCorpusError(
                f"重放源归档第{raw_page_number}页response_hash不匹配：{path}"
            )
        latest_by_page[raw_page_number] = (raw_response_hash, raw_page_json)

    page_one = latest_by_page.get(1)
    if page_one is None:
        raise ReplayCorpusError(f"重放源归档缺少第一页：{path}")
    page_one_object = _page_object(page_one[1], source=f"{path}第1页")
    raw_page_count = page_one_object.get("totalPage", 1)
    if type(raw_page_count) is not int or raw_page_count < 1:
        raise ReplayCorpusError(f"重放源归档第一页totalPage无效：{path}")

    missing_pages = [
        page_number
        for page_number in range(1, raw_page_count + 1)
        if page_number not in latest_by_page
    ]
    if missing_pages:
        preview = ", ".join(str(page) for page in missing_pages[:10])
        raise ReplayCorpusError(f"重放源归档缺少分页{preview}：{path}")

    selected_pages: dict[int, tuple[str, bytes]] = {}
    for page_number in range(1, raw_page_count + 1):
        response_hash, page_json = latest_by_page[page_number]
        page = _page_object(page_json, source=f"{path}第{page_number}页")
        current_page = page.get("currentPage")
        if current_page is not None and current_page != page_number:
            raise ReplayCorpusError(
                f"重放源归档currentPage与页号不一致：{path}第{page_number}页"
            )
        selected_pages[page_number] = (response_hash, page_json.encode("utf-8"))
    return _LatestArchivePages(
        page_count=raw_page_count,
        pages=selected_pages,
        database_state=database_state,
    )


def _optional_int(value: object, *, source: str) -> Optional[int]:
    if value is None:
        return None
    if type(value) is int:
        return value
    raise ReplayCorpusError(f"{source}不是整数或null：{value!r}")


def _build_synthetic_thread(
    path: Path,
    config: _ThreadReplayConfig,
    hasher: _Hasher,
    *,
    expected_database_state: _DatabaseState,
) -> _SyntheticThread:
    if config.aid is None:
        raise ReplayCorpusError("原帖归档不需要合成原帖响应。")
    try:
        connection, database_state = _connect_frozen(
            path,
            expected_state=expected_database_state,
        )
        with closing(connection):
            state_row = connection.execute(
                "SELECT tid, aid FROM floor_map_state WHERE singleton = 1"
            ).fetchone()
            if state_row != (config.tid, config.aid):
                raise ReplayCorpusError(f"重放源归档缺少匹配的楼层映射：{path}")
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

            posts_by_original_lou: dict[int, _SyntheticPost] = {}
            omitted_original_lous: set[int] = set()
            max_original_lou = -1
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
                if original_lou is not None:
                    max_original_lou = max(max_original_lou, original_lou)
                if pid is not None:
                    if original_lou is None:
                        raise ReplayCorpusError(
                            f"重放源存在未映射的作者帖子，无法合成原帖：{path}"
                        )
                    existing = posts_by_original_lou.get(original_lou)
                    synthetic_post = _SyntheticPost(pid, config.aid, "")
                    if existing is not None and existing != synthetic_post:
                        raise ReplayCorpusError(
                            f"多个作者帖子映射到同一原楼层{original_lou}：{path}"
                        )
                    posts_by_original_lou[original_lou] = synthetic_post
                    continue
                if original_lou is None:
                    continue
                if original_pid is None:
                    omitted_original_lous.add(original_lou)
                    continue

                content_row = connection.execute(
                    """
                    SELECT content
                    FROM post_versions
                    WHERE lou = ? AND pid = ?
                    ORDER BY last_seen_at DESC, id DESC
                    LIMIT 1
                    """,
                    (raw_author_lou, original_pid),
                ).fetchone()
                if content_row is None or not isinstance(content_row[0], str):
                    continue
                recovered_content = content_row[0]
                posts_by_original_lou[original_lou] = _SyntheticPost(
                    original_pid,
                    -1,
                    recovered_content,
                )
                _hash_fields(
                    hasher,
                    "recovered",
                    config.tid,
                    original_lou,
                    original_pid,
                    hashlib.sha256(recovered_content.encode("utf-8")).hexdigest(),
                )

            for raw_author_lou, raw_candidate_index, raw_original_lou in candidate_rows:
                if (
                    type(raw_author_lou) is not int
                    or type(raw_candidate_index) is not int
                    or type(raw_original_lou) is not int
                ):
                    raise ReplayCorpusError(f"重放源楼层候选行无效：{path}")
                _hash_fields(
                    hasher,
                    "candidate",
                    config.tid,
                    config.aid,
                    raw_author_lou,
                    raw_candidate_index,
                    raw_original_lou,
                )
                if raw_original_lou in posts_by_original_lou:
                    raise ReplayCorpusError(
                        "缺失楼候选原楼层与已恢复帖子冲突："
                        f"{path}: original_lou={raw_original_lou}"
                    )
                omitted_original_lous.add(raw_original_lou)
                max_original_lou = max(max_original_lou, raw_original_lou)
        _verify_frozen(path, database_state)
    except sqlite3.Error as error:
        raise ReplayCorpusError(f"无法读取重放楼层映射：{path}: {error}") from error

    configured_rows = 0 if config.replies is None else config.replies + 1
    row_count = max(1, configured_rows, max_original_lou + 1)
    return _SyntheticThread(
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
    exact_pages: dict[PageKey, bytes] = {}
    synthetic_threads: dict[int, _SyntheticThread] = {}
    exact_payload_bytes = 0
    total_configs = len(configs)
    total_work_items = total_configs + 1

    for index, config in enumerate(configs, start=1):
        if on_progress is not None:
            on_progress(index - 1, total_work_items, f"读取 tid={config.tid}")
        configured_archive = _archive_path(
            resolved_output,
            config.tid,
            config.aid,
        )
        latest_pages = _read_latest_archive_pages(configured_archive)
        for page_number, (response_hash, payload) in latest_pages.pages.items():
            key = (config.tid, config.aid, page_number)
            exact_pages[key] = payload
            exact_payload_bytes += len(payload)
            _hash_fields(
                hasher,
                "page",
                config.tid,
                config.aid,
                page_number,
                response_hash,
            )

        if config.aid is None:
            continue
        original_archive = _archive_path(resolved_output, config.tid, None)
        if original_archive.is_file():
            original_pages = _read_latest_archive_pages(original_archive)
            for page_number, (response_hash, payload) in original_pages.pages.items():
                key = (config.tid, None, page_number)
                exact_pages[key] = payload
                exact_payload_bytes += len(payload)
                _hash_fields(
                    hasher,
                    "page",
                    config.tid,
                    None,
                    page_number,
                    response_hash,
                )
        else:
            synthetic_threads[config.tid] = _build_synthetic_thread(
                configured_archive,
                config,
                hasher,
                expected_database_state=latest_pages.database_state,
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
        exact_page_count=len(exact_pages),
        exact_page_payload_bytes=exact_payload_bytes,
        synthetic_thread_count=len(synthetic_threads),
        image_mapping_count=image_result.mapping_count,
        available_image_mapping_count=len(image_result.images_by_url),
        unavailable_image_mapping_count=image_result.unavailable_mapping_count,
        unique_image_file_count=image_result.unique_file_count,
        unique_image_file_bytes=image_result.unique_file_bytes,
    )
    return ReplayCorpus(
        exact_pages=exact_pages,
        synthetic_threads=synthetic_threads,
        images_by_url=image_result.images_by_url,
        manifest=manifest,
    )
