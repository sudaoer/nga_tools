from __future__ import annotations

from typing import cast

from bs4 import BeautifulSoup, Tag

from nga_tools.web.image_usage import ImageProblemIssue, ImageProblemKind

_KIND_LABELS: dict[ImageProblemKind, str] = {
    "invalid_url": "链接无效",
    "unmapped": "未建立本地映射",
    "missing_file": "本地文件缺失",
}


def _append_issue_description(
    soup: BeautifulSoup,
    container: Tag,
    image_index: int,
    issue: ImageProblemIssue,
) -> None:
    heading = soup.new_tag("strong")
    heading.string = f"第{image_index}张图片 · {_KIND_LABELS[issue.kind]}"
    container.append(heading)

    url = soup.new_tag("code")
    url.string = issue.url
    container.append(url)

    if issue.relative_path is not None:
        path = soup.new_tag("span")
        path.string = f"映射：{issue.relative_path}"
        container.append(path)


def _open_ancestor_details(image: Tag) -> None:
    for ancestor in image.parents:
        if ancestor.name == "details":
            ancestor["open"] = ""


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
    located_indexes: set[int] = set()

    images = cast(list[Tag], soup.find_all("img"))
    for image_index, image in enumerate(images, start=1):
        issue = issues_by_image_index.get(image_index)
        if issue is None:
            continue

        wrapper = soup.new_tag("span")
        wrapper["class"] = f"image-problem-inline kind-{issue.kind}"
        warning = soup.new_tag("span")
        warning["class"] = "image-problem-inline-warning"
        _append_issue_description(soup, warning, image_index, issue)

        image.replace_with(wrapper)
        wrapper.append(warning)
        wrapper.append(image)
        _open_ancestor_details(image)
        located_indexes.add(image_index)

    missing_indexes = sorted(set(issues_by_image_index) - located_indexes)
    if missing_indexes:
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
                image_index,
                issues_by_image_index[image_index],
            )
            fallback.append(item)
        soup.insert(0, fallback)

    return str(soup)
