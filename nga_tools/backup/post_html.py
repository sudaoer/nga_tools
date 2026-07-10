from __future__ import annotations

from collections.abc import Sequence

from nga_tools.backup.floor_models import (
    MISSING_POST_HTML,
    AuthorPostRef,
    FloorLabels,
    RecoveredMissingPost,
)
from nga_tools.backup.models import PostData, PostHtml, PostRecord
from nga_tools.backup.post_data import (
    make_image_src_resolver,
    page_posts,
    post_source_hash,
)
from nga_tools.bbcode_convert import bbcode_to_html
from nga_tools.console import report_info, report_warning
from nga_tools.core.hashing import hash_object
from nga_tools.ngaclient.client import PageData


def post_html_from_content(post: PostData) -> str:
    return bbcode_to_html(
        post["content"],
        image_src_resolver=make_image_src_resolver(post["image_attachments"]),
    )


def missing_post_source_hash(post_html: str) -> str:
    return hash_object({"missing_post_html": post_html})


def prepare_post_records(page_data_by_page: dict[int, PageData]) -> list[PostRecord]:
    records: list[PostRecord] = []

    for page_number in sorted(page_data_by_page):
        for post in page_posts(page_data_by_page[page_number]):
            records.append(
                {
                    "lou": post["lou"],
                    "pid": post["pid"],
                    "post": post,
                    "html": None,
                    "source_hash": post_source_hash(post),
                }
            )

    return records


def load_post_htmls_for_records(records: list[PostRecord]) -> list[PostHtml]:
    htmls: list[PostHtml] = []
    for record in records:
        post_html = record["html"]
        if post_html is None:
            post = record["post"]
            if post is None:
                raise RuntimeError(f"缺少第{record['lou']}楼的可转换内容。")
            post_html = post_html_from_content(post)
            record["html"] = post_html
        htmls.append(
            {
                "lou": record["lou"],
                "pid": record["pid"],
                "html": post_html,
            }
        )
    return htmls


def build_post_htmls(page_data_by_page: dict[int, PageData]) -> list[PostHtml]:
    records = prepare_post_records(page_data_by_page)
    return load_post_htmls_for_records(records)


def find_missing_lou(
    posts: Sequence[PostHtml] | Sequence[PostRecord],
    total_lou_count: int | None = None,
) -> list[int]:
    """Find missing author lous; ``total_lou_count`` is NGA ``vrows`` count."""
    expected_lou = 1
    missing_lou: list[int] = []
    for item in sorted(posts, key=lambda post: post["lou"]):
        if item["lou"] != expected_lou:
            for lou in range(expected_lou, item["lou"]):
                missing_lou.append(lou)
            expected_lou = item["lou"]
        expected_lou += 1

    if total_lou_count is not None:
        # NGA author lous are 0-based; vrows is a row count, not the max lou.
        last_author_lou = total_lou_count - 1
        if last_author_lou >= 0 and expected_lou <= last_author_lou:
            missing_lou.extend(range(expected_lou, last_author_lou + 1))

    return missing_lou


def merge_missing_lou(*missing_lou_groups: list[int]) -> list[int]:
    return sorted(
        {
            lou
            for missing_lou_group in missing_lou_groups
            for lou in missing_lou_group
        }
    )


def fill_missing_lou(
    htmls: list[PostHtml],
    missing_lou: list[int],
    floor_labels: FloorLabels,
    recovered_missing_html_by_lou: dict[int, str],
) -> None:
    for lou in missing_lou:
        if lou in recovered_missing_html_by_lou:
            report_info(f"已恢复缺失{floor_labels.label(lou)}。")
        else:
            report_warning(f"缺失{floor_labels.label(lou)}！")

    for lou in missing_lou:
        htmls.append(
            {
                "lou": lou,
                "pid": None,
                "html": recovered_missing_html_by_lou.get(lou, MISSING_POST_HTML),
            }
        )

    htmls.sort(key=lambda item: item["lou"])


def fill_missing_post_records(
    records: list[PostRecord],
    missing_lou: list[int],
    floor_labels: FloorLabels,
    recovered_missing_html_by_lou: dict[int, str],
) -> None:
    for lou in missing_lou:
        if lou in recovered_missing_html_by_lou:
            report_info(f"已恢复缺失{floor_labels.label(lou)}。")
        else:
            report_warning(f"缺失{floor_labels.label(lou)}！")

    for lou in missing_lou:
        post_html = recovered_missing_html_by_lou.get(lou, MISSING_POST_HTML)
        records.append(
            {
                "lou": lou,
                "pid": None,
                "post": None,
                "html": post_html,
                "source_hash": missing_post_source_hash(post_html),
            }
        )

    records.sort(key=lambda item: item["lou"])


def recovered_missing_post_htmls(
    recovered_missing_posts: dict[int, RecoveredMissingPost],
) -> dict[int, str]:
    if not recovered_missing_posts:
        return {}

    recovered_html_by_lou: dict[int, str] = {}
    for author_lou, recovered_post in sorted(recovered_missing_posts.items()):
        recovered_html_by_lou[author_lou] = bbcode_to_html(recovered_post["content"])
    return recovered_html_by_lou


def source_hashes_by_lou(records: Sequence[PostRecord]) -> dict[int, str]:
    return {record["lou"]: record["source_hash"] for record in records}


def unresolved_missing_placeholder_lous(records: Sequence[PostRecord]) -> set[int]:
    return {
        record["lou"]
        for record in records
        if record["pid"] is None and record["html"] == MISSING_POST_HTML
    }


def post_refs_from_posts(
    posts: Sequence[PostHtml] | Sequence[PostRecord],
) -> list[AuthorPostRef]:
    post_refs: list[AuthorPostRef] = []
    for item in posts:
        pid = item["pid"]
        if pid is None:
            continue
        post_refs.append({"pid": pid, "author_lou": item["lou"]})
    return post_refs


def post_refs_from_htmls(htmls: list[PostHtml]) -> list[AuthorPostRef]:
    return post_refs_from_posts(htmls)
