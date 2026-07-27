from __future__ import annotations

from typing import cast

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from nga_tools.web.image_usage import ImageProblemIssue, ImageProblemKind

_KIND_LABELS: dict[ImageProblemKind, str] = {
    ImageProblemKind.INVALID_URL: "链接无效",
    ImageProblemKind.UNMAPPED: "未建立本地映射",
    ImageProblemKind.MISSING_FILE: "本地文件缺失",
}


def _append_issue_description(
    soup: BeautifulSoup,
    container: Tag,
    location_label: str,
    issue: ImageProblemIssue,
) -> None:
    heading = soup.new_tag("strong")
    heading.string = f"{location_label} · {_KIND_LABELS[issue.kind]}"
    container.append(heading)

    url = soup.new_tag("code")
    url.string = issue.url
    container.append(url)

    if issue.relative_path is not None:
        path = soup.new_tag("span")
        path.string = f"映射：{issue.relative_path}"
        container.append(path)


def _open_ancestor_details(element: Tag) -> None:
    for ancestor in element.parents:
        if ancestor.name == "details":
            ancestor["open"] = ""


def _inside_problem_marker(node: NavigableString) -> bool:
    for parent in node.parents:
        classes = parent.get("class")
        if isinstance(classes, str):
            if "image-problem-inline" in classes.split():
                return True
        elif classes is not None and "image-problem-inline" in cast(
            list[str], classes
        ):
            return True
    return False


def _source_text_range(
    soup: BeautifulSoup,
    source: str,
) -> tuple[NavigableString, int, int] | None:
    for node in cast(list[NavigableString], soup.find_all(string=True)):
        if _inside_problem_marker(node):
            continue
        text = str(node)
        lowered = text.lower()
        if source:
            source_start = text.find(source)
            if source_start < 0:
                continue
            opening_start = lowered.rfind("[img]", 0, source_start + 1)
            if opening_start < 0:
                continue
            between = text[opening_start + len("[img]") : source_start]
            if between.strip():
                continue
            source_end = source_start + len(source)
            if (
                source_end < len(text)
                and not text[source_end].isspace()
                and text[source_end] not in "[<"
            ):
                continue
            return node, opening_start, source_end

        opening_start = lowered.find("[img]")
        if opening_start >= 0:
            return node, opening_start, opening_start + len("[img]")
    return None


def _wrap_source_occurrence(
    soup: BeautifulSoup,
    source_index: int,
    issue: ImageProblemIssue,
) -> bool:
    text_range = _source_text_range(soup, issue.url)
    if text_range is None:
        return False

    node, start, end = text_range
    text = str(node)
    wrapper = soup.new_tag("span")
    wrapper["class"] = f"image-problem-inline kind-{issue.kind}"
    warning = soup.new_tag("span")
    warning["class"] = "image-problem-inline-warning"
    _append_issue_description(
        soup,
        warning,
        f"第{source_index}个 [img]",
        issue,
    )
    source_text = soup.new_tag("span")
    source_text["class"] = "image-problem-inline-source"
    source_text.string = text[start:end]
    wrapper.append(warning)
    wrapper.append(source_text)

    node.replace_with(wrapper)
    if text[:start]:
        wrapper.insert_before(NavigableString(text[:start]))
    if text[end:]:
        wrapper.insert_after(NavigableString(text[end:]))
    _open_ancestor_details(wrapper)
    return True


def annotate_image_problem_html(
    html: str,
    issues: tuple[ImageProblemIssue, ...],
) -> str:
    """Add controlled diagnostics to already-sanitized post HTML."""

    soup = BeautifulSoup(html, "html.parser")
    issues_by_image_index = {
        image_index: issue
        for issue in issues
        for image_index in issue.image_indexes
    }
    issues_by_source_index = {
        source_index: issue
        for issue in issues
        for source_index in issue.source_indexes
    }
    located_indexes: set[int] = set()
    located_source_indexes: set[int] = set()

    images = cast(list[Tag], soup.find_all("img"))
    for image_index, image in enumerate(images, start=1):
        issue = issues_by_image_index.get(image_index)
        if issue is None:
            continue

        wrapper = soup.new_tag("span")
        wrapper["class"] = f"image-problem-inline kind-{issue.kind}"
        warning = soup.new_tag("span")
        warning["class"] = "image-problem-inline-warning"
        _append_issue_description(
            soup,
            warning,
            f"第{image_index}张图片",
            issue,
        )
        image.replace_with(wrapper)
        wrapper.append(warning)
        wrapper.append(image)
        _open_ancestor_details(image)
        located_indexes.add(image_index)

    for source_index, issue in sorted(issues_by_source_index.items()):
        if _wrap_source_occurrence(soup, source_index, issue):
            located_source_indexes.add(source_index)

    missing_indexes = sorted(set(issues_by_image_index) - located_indexes)
    missing_source_indexes = sorted(
        set(issues_by_source_index) - located_source_indexes
    )
    if missing_indexes or missing_source_indexes:
        fallback = soup.new_tag("div")
        fallback["class"] = "image-problem-unlocated"
        fallback_heading = soup.new_tag("strong")
        fallback_heading.string = "部分问题图片未能在当前渲染正文中定位"
        fallback.append(fallback_heading)
        for image_index in missing_indexes:
            item = soup.new_tag("span")
            _append_issue_description(
                soup,
                item,
                f"第{image_index}张图片",
                issues_by_image_index[image_index],
            )
            fallback.append(item)
        for source_index in missing_source_indexes:
            item = soup.new_tag("span")
            _append_issue_description(
                soup,
                item,
                f"第{source_index}个 [img]",
                issues_by_source_index[source_index],
            )
            fallback.append(item)
        soup.insert(0, fallback)

    return str(soup)
