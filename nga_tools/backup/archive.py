from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from nga_tools import utils
from nga_tools.backup import backup_state, html_modified_manifest
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_map import (
    AuthorPostRef,
    FloorMapBuildResult,
    FloorLabels,
    build_and_save_floor_map,
    load_floor_map_build_result_if_current,
    load_floor_labels,
    read_missing_author_lous_from_html_modified,
)
from nga_tools.backup import image_store
from nga_tools.backup.image_pipeline import (
    collect_image_download_tasks_from_parsed as _collect_image_download_tasks_from_parsed,
    completed_html_modified_lous_for_records as _completed_html_modified_lous_for_records,
    download_images as _download_images,
    failed_image_urls as _failed_image_urls,
    parse_post_htmls_for_images as _parse_post_htmls_for_images,
    rewrite_parsed_image_links as _rewrite_parsed_image_links,
    write_modified_htmls as _write_modified_htmls,
)
from nga_tools.backup.models import (
    PostHtml,
    PostRecord,
)
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
    post_refs_from_htmls as _post_refs_from_htmls,
    post_refs_from_posts as _post_refs_from_posts,
    recovered_missing_post_htmls as _recovered_missing_post_htmls,
    source_hashes_by_lou as _source_hashes_by_lou,
    unresolved_missing_placeholder_lous as _unresolved_missing_placeholder_lous,
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


def _can_fast_skip_author_backup(
    tid: int,
    aid: Optional[int],
    author_total_lou_count: int | None,
    archive_page_numbers: set[int],
) -> bool:
    if aid is None or author_total_lou_count is None:
        return False
    if not archive_page_numbers:
        return False

    thread_folder = Path(utils.get_folder(tid, aid))
    state = backup_state.load_state(thread_folder)
    if state is None:
        return False
    if state["author_total_lou_count"] != author_total_lou_count:
        return False

    expected_pages = set(range(1, state["page_count"] + 1))
    if not expected_pages <= archive_page_numbers:
        return False

    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    html_modified_entries = html_modified_manifest.load_manifest(folder_html_modified)
    if len(html_modified_entries) != state["html_modified_manifest_entry_count"]:
        return False
    if not html_modified_manifest.manifest_files_exist(
        folder_html_modified,
        html_modified_entries,
    ):
        return False

    return (thread_folder / "floor_map.json").is_file()


def _write_backup_state_if_complete(
    tid: int,
    aid: Optional[int],
    page_count: int,
    author_total_lou_count: int | None,
    records: Sequence[PostRecord],
    missing_lou: Sequence[int],
    source_hash_by_lou: dict[int, str],
    skipped_lous: set[int],
    completed_lous: set[int],
) -> None:
    if aid is None or author_total_lou_count is None:
        return
    unresolved_missing_lous = _unresolved_missing_placeholder_lous(records)
    if unresolved_missing_lous:
        return
    if set(source_hash_by_lou) - (skipped_lous | completed_lous):
        return
    if (
        load_floor_map_build_result_if_current(
            tid,
            aid,
            _post_refs_from_posts(records),
            missing_lou,
        )
        is None
    ):
        return

    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    html_modified_entries = html_modified_manifest.load_manifest(folder_html_modified)
    if len(html_modified_entries) != len(source_hash_by_lou):
        return
    if not html_modified_manifest.manifest_files_exist(
        folder_html_modified,
        html_modified_entries,
    ):
        return

    backup_state.write_state(
        Path(utils.get_folder(tid, aid)),
        author_total_lou_count=author_total_lou_count,
        page_count=page_count,
        html_modified_manifest_entry_count=len(html_modified_entries),
        unresolved_missing_count=0,
    )


def _build_floor_map_for_backup(  # pyright: ignore[reportUnusedFunction]
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    htmls: list[PostHtml],
    missing_lou: list[int],
) -> FloorMapBuildResult:
    return _build_floor_map_for_post_refs(
        client,
        tid,
        aid,
        _post_refs_from_htmls(htmls),
        missing_lou,
    )


def _build_floor_map_for_post_refs(
    client: NGAClient,
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
                tid,
                aid,
                post_refs,
                missing_lou,
            )
            if current_result is not None:
                report_info("楼层映射输入未变化，复用已有floor_map.json。")
                return current_result
        return build_and_save_floor_map(
            client,
            tid,
            aid,
            post_refs,
            missing_lou,
            strict=False,
        )
    except Exception as error:
        report_warning(f"楼层映射生成失败，继续生成备份：{error}")
        try:
            floor_labels = load_floor_labels(tid, aid)
        except Exception as load_error:
            report_warning(f"无法加载已有楼层映射，使用普通楼层标签：{load_error}")
            floor_labels = FloorLabels.plain()
        return FloorMapBuildResult(floor_labels, {})


def backup_thread(
    tid: int,
    aid: Optional[int],
    *,
    write_json: bool = False,
) -> None:
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

    report_info("开始处理")

    with time_section("读取归档与楼层映射"):
        records = archive_store.read_latest_post_records()
        missing_lou = _find_missing_lou(records)
        floor_map_result = _build_floor_map_for_post_refs(
            client,
            tid,
            aid,
            _post_refs_from_posts(records),
            missing_lou,
        )
        floor_labels = floor_map_result.floor_labels
        recovered_missing_html_by_lou = _recovered_missing_post_htmls(
            floor_map_result.recovered_missing_posts_by_author_lou,
        )

    with time_section("HTML与图片处理"):
        _fill_missing_post_records(
            records,
            missing_lou,
            floor_labels,
            recovered_missing_html_by_lou,
        )
        folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
        source_hash_by_lou = _source_hashes_by_lou(records)
        htmls = _load_post_htmls_for_records(records)
        parsed_htmls = _parse_post_htmls_for_images(htmls)
        files_to_download = _collect_image_download_tasks_from_parsed(
            parsed_htmls,
            floor_labels,
        )
        download_result = _download_images(tid, aid, files_to_download)
        image_lookup = image_store.ImageLookupCache.for_tasks(files_to_download)
        completed_lous = set(
            _rewrite_parsed_image_links(
                parsed_htmls,
                tid,
                aid,
                floor_labels,
                _failed_image_urls(download_result),
                image_lookup,
            )
        )
        unresolved_missing_lous = _unresolved_missing_placeholder_lous(records)
        completed_lous -= unresolved_missing_lous
        output_hash_by_lou = _write_modified_htmls(htmls, tid, aid)

    with time_section("manifest/state写入"):
        html_modified_manifest.write_updated_manifest(
            folder_html_modified,
            previous_entries={},
            source_hash_by_lou=source_hash_by_lou,
            skipped_lous=set(),
            completed_lous=completed_lous,
            output_hash_by_lou=output_hash_by_lou,
        )
        _write_backup_state_if_complete(
            tid,
            aid,
            page_count,
            author_total_lou_count,
            records,
            missing_lou,
            source_hash_by_lou,
            skipped_lous=set(),
            completed_lous=completed_lous,
        )


def backup_thread_sub(
    tid: int,
    aid: Optional[int],
    *,
    write_json: bool = False,
) -> None:
    client = NGAClient()
    with time_section("增量预检查"):
        thread_folder = Path(utils.get_folder(tid, aid))
        archive_store = ThreadArchiveStore(thread_folder)
        existing_page_numbers = archive_store.read_page_numbers()
        _ensure_legacy_json_is_migrated(tid, aid, archive_store, existing_page_numbers)

        first_page_data = client.get_page(tid, aid, 1)
        page_count = _page_count_from_page_data(first_page_data)
        author_total_lou_count = _author_total_lou_count_from_page_data(
            first_page_data,
            aid,
        )
        if _can_fast_skip_author_backup(
            tid,
            aid,
            author_total_lou_count,
            existing_page_numbers,
        ):
            report_info("只看楼主总楼数未变化，跳过增量处理。")
            return

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
        records = archive_store.read_latest_post_records()
        missing_lou = _find_missing_lou(records)
        if aid is not None:
            present_lou = {item["lou"] for item in records}
            previous_missing_lou = [
                lou
                for lou in read_missing_author_lous_from_html_modified(tid, aid)
                if lou not in present_lou
            ]
            missing_lou = _merge_missing_lou(missing_lou, previous_missing_lou)
        floor_map_result = _build_floor_map_for_post_refs(
            client,
            tid,
            aid,
            _post_refs_from_posts(records),
            missing_lou,
        )
        floor_labels = floor_map_result.floor_labels
        recovered_missing_html_by_lou = _recovered_missing_post_htmls(
            floor_map_result.recovered_missing_posts_by_author_lou,
        )

    with time_section("HTML与图片处理"):
        _fill_missing_post_records(
            records,
            missing_lou,
            floor_labels,
            recovered_missing_html_by_lou,
        )
        (
            folder_html_modified,
            source_hash_by_lou,
            manifest_entries,
            skipped_lous,
        ) = _completed_html_modified_lous_for_records(records, tid, aid)
        unresolved_missing_lous = _unresolved_missing_placeholder_lous(records)
        skipped_lous -= unresolved_missing_lous
        active_records = [item for item in records if item["lou"] not in skipped_lous]
        active_htmls = _load_post_htmls_for_records(active_records)

        parsed_htmls = _parse_post_htmls_for_images(active_htmls)
        files_to_download = _collect_image_download_tasks_from_parsed(
            parsed_htmls,
            floor_labels,
        )
        download_result = _download_images(tid, aid, files_to_download)
        image_lookup = image_store.ImageLookupCache.for_tasks(files_to_download)
        completed_lous = set(
            _rewrite_parsed_image_links(
                parsed_htmls,
                tid,
                aid,
                floor_labels,
                _failed_image_urls(download_result),
                image_lookup,
            )
        )
        completed_lous -= unresolved_missing_lous
        output_hash_by_lou = _write_modified_htmls(active_htmls, tid, aid)

    with time_section("manifest/state写入"):
        html_modified_manifest.write_updated_manifest(
            folder_html_modified,
            previous_entries=manifest_entries,
            source_hash_by_lou=source_hash_by_lou,
            skipped_lous=skipped_lous,
            completed_lous=completed_lous,
            output_hash_by_lou=output_hash_by_lou,
        )
        _write_backup_state_if_complete(
            tid,
            aid,
            page_count,
            author_total_lou_count,
            records,
            missing_lou,
            source_hash_by_lou,
            skipped_lous=skipped_lous,
            completed_lous=completed_lous,
        )
