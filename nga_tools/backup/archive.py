from __future__ import annotations

from pathlib import Path
from typing import Optional

from nga_tools import utils
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_map import (
    AuthorPostRef,
    FloorMapBuildResult,
    FloorLabels,
    build_and_save_floor_map,
    find_missing_author_lous,
    load_floor_map_build_result_if_current,
    load_floor_labels_from_archive,
    read_unresolved_missing_author_lous_from_archive,
)
from nga_tools.backup.image_pipeline import (
    collect_image_download_tasks_from_parsed as _collect_image_download_tasks_from_parsed,
    download_images as _download_images,
    parse_post_htmls_for_images as _parse_post_htmls_for_images,
)
from nga_tools.backup.models import PostRecord
from nga_tools.backup.page_store import (
    author_total_lou_count_from_page_data as _author_total_lou_count_from_page_data,
    fetch_backup_pages as _fetch_backup_pages,
    fetch_backup_page as _fetch_backup_page,
    page_count_from_page_data as _page_count_from_page_data,
    write_page_json as _write_page_json,
)
from nga_tools.backup.post_html import (
    fill_missing_post_records as _fill_missing_post_records,
    find_missing_lou as _find_missing_lou,
    load_post_htmls_for_records as _load_post_htmls_for_records,
    merge_missing_lou as _merge_missing_lou,
    post_refs_from_posts as _post_refs_from_posts,
)
from nga_tools.backup.post_overlay import (
    apply_post_overlays_to_records as _apply_post_overlays_to_records,
)
from nga_tools.backup.floor_models import PAGE_JSON_RE
from nga_tools.console import report_info, report_progress, report_warning
from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import PageData
from nga_tools.timing import time_section


def _upsert_archive_pages(
    store: ThreadArchiveStore,
    page_data_by_page: dict[int, PageData],
) -> None:
    for page_number in sorted(page_data_by_page):
        store.upsert_page(page_number, page_data_by_page[page_number])


def _legacy_page_numbers(folder_json: Path) -> set[int]:
    if not folder_json.is_dir():
        return set()
    page_numbers: set[int] = set()
    for path in folder_json.iterdir():
        if not path.is_file():
            continue
        match = PAGE_JSON_RE.fullmatch(path.name)
        if match is not None:
            page_numbers.add(int(match.group(1)))
    return page_numbers


def _archive_migration_command(tid: int, aid: Optional[int]) -> str:
    command = f"backup migrate-store --tid {tid}"
    if aid is not None:
        command += f" --aid {aid}"
    return command


def _ensure_legacy_json_is_migrated(
    tid: int,
    aid: Optional[int],
    archive_store: ThreadArchiveStore,
    archive_page_numbers: set[int],
) -> None:
    folder_json = archive_store.thread_folder / "json"
    legacy_page_numbers = _legacy_page_numbers(folder_json)
    unmigrated_page_numbers = legacy_page_numbers - archive_page_numbers
    if not unmigrated_page_numbers:
        return

    raise RuntimeError(
        f"{archive_store.db_path} 未覆盖旧JSON页："
        f"{', '.join(str(item) for item in sorted(unmigrated_page_numbers))}。"
        "正常备份不再读取旧JSON；请先运行 "
        f"{_archive_migration_command(tid, aid)}。"
    )


def _build_floor_map_for_post_refs(
    client: NGAClient,
    archive_store: ThreadArchiveStore,
    tid: int,
    aid: Optional[int],
    post_refs: list[AuthorPostRef],
    missing_lou: list[int],
) -> FloorMapBuildResult:
    if aid is None:
        return FloorMapBuildResult(FloorLabels.plain(), {})

    try:
        if not missing_lou:
            current_result = load_floor_map_build_result_if_current(
                archive_store,
                post_refs,
                missing_lou,
            )
            if current_result is not None:
                report_info("楼层映射输入未变化，复用数据库中的已有映射。")
                return current_result
        return build_and_save_floor_map(
            client,
            archive_store,
            tid,
            aid,
            post_refs,
            missing_lou,
            strict=False,
        )
    except Exception as error:
        report_warning(f"楼层映射生成失败，继续生成备份：{error}")
        try:
            floor_labels = load_floor_labels_from_archive(archive_store, aid)
        except Exception as load_error:
            report_warning(f"无法加载已有楼层映射，使用普通楼层标签：{load_error}")
            floor_labels = FloorLabels.plain()
        return FloorMapBuildResult(floor_labels, {})


def _post_refs_and_missing_lous(
    archive_store: ThreadArchiveStore,
    aid: Optional[int],
    author_total_lou_count: int | None,
    records: list[PostRecord],
) -> tuple[list[AuthorPostRef], list[int]]:
    if aid is None:
        return (
            _post_refs_from_posts(records),
            _find_missing_lou(records, author_total_lou_count),
        )

    post_refs = archive_store.read_latest_author_post_refs()
    present_lous = {post["author_lou"] for post in post_refs}
    missing_lous = find_missing_author_lous(
        post_refs,
        author_total_lou_count,
    )
    previous_missing_lous = read_unresolved_missing_author_lous_from_archive(
        archive_store,
        present_lous=present_lous,
        total_lou_count=author_total_lou_count,
    )
    return post_refs, _merge_missing_lou(missing_lous, previous_missing_lous)


def _records_with_recovered_and_missing_posts(
    archive_store: ThreadArchiveStore,
    floor_map_result: FloorMapBuildResult,
    missing_lous: list[int],
) -> list[PostRecord]:
    archive_store.upsert_recovered_posts(
        floor_map_result.recovered_missing_posts_by_author_lou
    )
    records = archive_store.read_effective_post_records()
    present_lous = {record["lou"] for record in records}
    unresolved_missing_lous = [
        lou for lou in missing_lous if lou not in present_lous
    ]
    _fill_missing_post_records(
        records,
        unresolved_missing_lous,
        floor_map_result.floor_labels,
    )
    return records


def _download_images_for_records(
    tid: int,
    aid: Optional[int],
    thread_folder: Path,
    floor_labels: FloorLabels,
    records: list[PostRecord],
) -> None:
    with time_section("Overlay应用"):
        effective_records = _apply_post_overlays_to_records(thread_folder, records)
    with time_section("BBCode转临时HTML"):
        htmls = _load_post_htmls_for_records(effective_records)
    with time_section("图片解析与任务收集"):
        parsed_htmls = _parse_post_htmls_for_images(htmls)
        files_to_download = _collect_image_download_tasks_from_parsed(
            parsed_htmls,
            floor_labels,
        )
    _download_images(tid, aid, files_to_download)


def backup_thread(
    tid: int,
    aid: Optional[int],
    *,
    write_json: bool = False,
) -> None:
    with time_section("客户端初始化"):
        client = NGAClient()
    with time_section("抓取和写入页面"):
        thread_folder = Path(utils.get_folder(tid, aid))
        archive_store = ThreadArchiveStore(thread_folder)
        first_page_data = client.get_page(tid, aid, 1)
        page_count = _page_count_from_page_data(first_page_data)
        author_total_lou_count = _author_total_lou_count_from_page_data(
            first_page_data,
            aid,
        )

        page_data_by_page = _fetch_backup_pages(
            client,
            tid,
            aid,
            page_count,
            first_page_data,
            write_json=write_json,
        )
        _upsert_archive_pages(archive_store, page_data_by_page)
        archive_store.refresh_stored_word_counts()

    report_info("开始处理")

    with time_section("读取归档与楼层映射"):
        with time_section("读取完整归档记录"):
            records = archive_store.read_effective_post_records()
        with time_section("缺失楼读取与合并"):
            post_refs, missing_lous = _post_refs_and_missing_lous(
                archive_store,
                aid,
                author_total_lou_count,
                records,
            )
        with time_section("楼层映射生成/复用"):
            floor_map_result = _build_floor_map_for_post_refs(
                client,
                archive_store,
                tid,
                aid,
                post_refs,
                missing_lous,
            )
        with time_section("恢复正文写入与缺失楼合并"):
            records = _records_with_recovered_and_missing_posts(
                archive_store,
                floor_map_result,
                missing_lous,
            )

    with time_section("正文解析与图片处理"):
        _download_images_for_records(
            tid,
            aid,
            thread_folder,
            floor_map_result.floor_labels,
            records,
        )


def backup_thread_sub(
    tid: int,
    aid: Optional[int],
    *,
    write_json: bool = False,
) -> None:
    with time_section("客户端初始化"):
        client = NGAClient()
    with time_section("增量预检查"):
        thread_folder = Path(utils.get_folder(tid, aid))
        archive_store = ThreadArchiveStore(thread_folder)
        existing_page_numbers = archive_store.read_page_numbers()
        _ensure_legacy_json_is_migrated(tid, aid, archive_store, existing_page_numbers)
        if archive_store.exists():
            archive_store.refresh_stored_word_counts()

        first_page_data = client.get_page(tid, aid, 1)
        page_count = _page_count_from_page_data(first_page_data)
        author_total_lou_count = _author_total_lou_count_from_page_data(
            first_page_data,
            aid,
        )

        if existing_page_numbers:
            tail_start = min(max(existing_page_numbers), page_count)
        else:
            tail_start = 1
        missing_page_numbers = set(range(1, page_count + 1)) - existing_page_numbers
        refresh_page_numbers = (
            set(range(tail_start, page_count + 1)) | missing_page_numbers
        )
        folder_json = Path(utils.get_folder(tid, aid, "json")) if write_json else None

    with time_section("抓取和写入页面"):
        report_progress(
            f"准备增量备份：远端{page_count}页，本地{len(existing_page_numbers)}页，"
            f"需获取{len(refresh_page_numbers)}页",
            completed=0,
            total=len(refresh_page_numbers),
        )
        sorted_refresh_page_numbers = sorted(refresh_page_numbers)
        for index, page_number in enumerate(sorted_refresh_page_numbers, start=1):
            report_progress(
                f"正在获取第{page_number}页",
                completed=index - 1,
                total=len(sorted_refresh_page_numbers),
            )
            page_data = _fetch_backup_page(
                client,
                tid,
                aid,
                page_number,
                page_count,
                first_page_data,
            )
            if folder_json is not None:
                _write_page_json(folder_json, page_number, page_data)
            archive_store.upsert_page(page_number, page_data)
        report_progress(
            "页面获取完成",
            completed=len(sorted_refresh_page_numbers),
            total=len(sorted_refresh_page_numbers),
        )

    report_info("开始处理")

    with time_section("读取归档与楼层映射"):
        with time_section("读取完整归档记录"):
            records = archive_store.read_effective_post_records()
        with time_section("缺失楼读取与合并"):
            post_refs, missing_lous = _post_refs_and_missing_lous(
                archive_store,
                aid,
                author_total_lou_count,
                records,
            )
        with time_section("楼层映射生成/复用"):
            floor_map_result = _build_floor_map_for_post_refs(
                client,
                archive_store,
                tid,
                aid,
                post_refs,
                missing_lous,
            )
        with time_section("恢复正文写入与缺失楼合并"):
            records = _records_with_recovered_and_missing_posts(
                archive_store,
                floor_map_result,
                missing_lous,
            )

    with time_section("正文解析与图片处理"):
        _download_images_for_records(
            tid,
            aid,
            thread_folder,
            floor_map_result.floor_labels,
            records,
        )
