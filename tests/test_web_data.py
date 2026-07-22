from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from PIL import Image

from nga_tools.backup.archive_post_store import ArchivePostRepository
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
    StoredFloorMap,
)
from nga_tools.core.hashing import hash_text
from nga_tools.forum.thread_configs import ThreadConfig
from nga_tools.web.output_files import safe_output_file
from nga_tools.web.post_data import read_post_version_preview, read_posts
from nga_tools.web.thread_data import scan_thread_summaries
from tests.web_viewer_support import (
    _post,
    _write_archive,
    _write_audio_mapping,
    _write_image_mapping,
)


class WebViewerDataTest:
    def test_scans_ready_and_omits_json_only_backups(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        ready_dir = output_dir / "101_201"
        legacy_dir = output_dir / "102_all"
        _write_archive(ready_dir, [_post(1, "hello"), _post(2, "world")])
        (legacy_dir / "json").mkdir(parents=True)
        (legacy_dir / "json" / "page_1.json").write_text("{}", encoding="utf-8")
        metadata: ThreadConfig = {
            "thread_name": "sample",
            "tid": 101,
            "aid": 201,
            "subject": "Sample Thread",
            "author": "Alice",
            "link": "https://bbs.nga.cn/read.php?tid=101",
            "replies": 2,
            "postdate": 1783400000,
            "lastpost": 1783490000,
        }

        summaries = scan_thread_summaries(output_dir, {(101, "201"): metadata})
        by_dir = {item["dirName"]: item for item in summaries}

        assert by_dir["101_201"]["status"] == "ready"
        assert by_dir["101_201"]["statsLoaded"] is True
        assert by_dir["101_201"]["threadName"] == "sample"
        assert by_dir["101_201"]["subject"] == "Sample Thread"
        assert by_dir["101_201"]["postCount"] == 2
        assert by_dir["101_201"]["bodyWordCount"] == 0
        assert by_dir["101_201"]["bodyChineseCharCount"] == 0
        assert by_dir["101_201"]["bodyWordPostCount"] == 0
        assert by_dir["101_201"]["minLou"] == 1
        assert by_dir["101_201"]["maxLou"] == 2
        assert by_dir["101_201"]["authorUpdatedAt"] == 1783490002
        assert by_dir["101_201"]["link"] == (
            "https://bbs.nga.cn/read.php?tid=101&authorid=201"
        )
        assert "hasHtmlModified" not in by_dir["101_201"]
        assert "102_all" not in by_dir

    def test_full_scan_rejects_unsupported_archive_schema(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "hello")])
        with closing(sqlite3.connect(thread_dir / "archive.sqlite3")) as connection:
            connection.execute("PRAGMA user_version = 0")
            connection.commit()

        summaries = scan_thread_summaries(output_dir, {}, detail="full")

        assert len(summaries) == 1
        assert summaries[0]["status"] == "invalid"
        assert summaries[0]["message"] is not None
        assert "版本不受支持" in summaries[0]["message"]

    def test_scans_stored_word_count_summary(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        ThreadArchiveStore(thread_dir).ingest.upsert_page(
            1,
            {
                "result": [
                    _post(1, "正文，" * 40),
                    _post(2, "短。"),
                ]
            },
        )

        summaries = scan_thread_summaries(output_dir, {})
        summary = summaries[0]

        assert summary["bodyWordCount"] == 120
        assert summary["bodyChineseCharCount"] == 80
        assert summary["bodyWordPostCount"] == 1

    def test_reads_fixed_floor_slots_without_shifting_missing_floors(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(
            thread_dir,
            [
                _post(0, "floor zero"),
                _post(2, "floor two"),
            ],
        )

        result = read_posts(output_dir, 101, "201", page=1)

        assert result["page"] == 1
        assert result["pageStartLou"] == 0
        assert result["pageEndLou"] == 2
        assert len(result["slots"]) == 3
        assert result["slots"][0]["lou"] == 0
        assert result["slots"][0]["emptyReason"] is None
        assert result["slots"][1]["lou"] == 1
        assert result["slots"][1]["emptyReason"] == "missing"
        assert result["slots"][2]["lou"] == 2
        assert result["slots"][2]["emptyReason"] is None
        assert [item["lou"] for item in result["items"]] == [0, 2]

    def test_reads_last_page_without_tail_missing_slots(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(19, "page one end"), _post(21, "last")])

        result = read_posts(output_dir, 101, "201", page=2)

        assert result["pageStartLou"] == 20
        assert result["pageEndLou"] == 21
        assert [slot["lou"] for slot in result["slots"]] == [20, 21]
        assert result["slots"][0]["emptyReason"] == "missing"
        assert result["slots"][1]["emptyReason"] is None

    def test_reads_unfiltered_page_without_loading_full_thread(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "early"), _post(25, "target")])
        calls: list[set[int] | None] = []
        original = ArchivePostRepository.read_effective_post_rows

        def wrapped_read_effective_post_rows(
            store: ArchivePostRepository,
            lous: set[int] | None = None,
        ):
            calls.append(None if lous is None else set(lous))
            return original(store, lous)

        monkeypatch.setattr(
            ArchivePostRepository,
            "read_effective_post_rows",
            wrapped_read_effective_post_rows,
        )

        result = read_posts(output_dir, 101, "201", page=2)

        assert calls == [set(range(20, 26))]
        assert result["postCount"] == 2
        assert result["matchingPostCount"] == 2
        assert [item["lou"] for item in result["items"]] == [25]

    def test_reads_filtered_page_by_matching_floor(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(
            thread_dir,
            [_post(1, "needle early"), _post(25, "needle target")],
        )
        calls: list[set[int] | None] = []
        original = ArchivePostRepository.read_effective_post_rows

        def wrapped_read_effective_post_rows(
            store: ArchivePostRepository,
            lous: set[int] | None = None,
        ):
            calls.append(None if lous is None else set(lous))
            return original(store, lous)

        monkeypatch.setattr(
            ArchivePostRepository,
            "read_effective_post_rows",
            wrapped_read_effective_post_rows,
        )

        result = read_posts(output_dir, 101, "201", page=1, query="needle")

        assert calls == [None]
        assert result["matchingPostCount"] == 2
        assert result["total"] == 2
        assert result["totalPages"] == 1
        assert result["pageStartLou"] == 1
        assert result["pageEndLou"] == 25
        assert [slot["lou"] for slot in result["slots"]] == [1, 25]
        assert [item["lou"] for item in result["items"]] == [1, 25]

    def test_reads_filtered_pages_by_matching_floor_order(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(
            thread_dir,
            [_post(lou, "needle") for lou in range(1, 22)],
        )

        result = read_posts(output_dir, 101, "201", page=2, query="needle")

        assert result["page"] == 2
        assert result["total"] == 21
        assert result["totalPages"] == 2
        assert result["offset"] == 20
        assert result["pageStartLou"] == 21
        assert result["pageEndLou"] == 21
        assert [item["lou"] for item in result["items"]] == [21]
        assert all(slot["emptyReason"] is None for slot in result["slots"])

    def test_reads_filtered_page_with_floor_range(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(
            thread_dir,
            [
                _post(1, "needle"),
                _post(2, "needle"),
                _post(3, "other"),
                _post(4, "needle"),
            ],
        )

        result = read_posts(
            output_dir,
            101,
            "201",
            page=1,
            query="needle",
            lou_from=2,
            lou_to=4,
        )

        assert result["matchingPostCount"] == 2
        assert [item["lou"] for item in result["items"]] == [2, 4]

    def test_reads_filtered_page_without_matches(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "other")])

        result = read_posts(output_dir, 101, "201", page=1, query="needle")

        assert result["total"] == 0
        assert result["totalPages"] == 1
        assert result["pageStartLou"] == 0
        assert result["pageEndLou"] == 0
        assert result["slots"] == []
        assert result["items"] == []

    def test_reads_posts_and_rewrites_local_image_sources(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        image_url = "https://img.nga.178.com/attachments/mon_202607/08/abc.png"
        _write_archive(
            thread_dir,
            [
                _post(1, f"hello image[img]{image_url}[/img]"),
                _post(2, "not matched"),
            ],
        )
        image_dir = output_dir / "images_unique"
        image_dir.mkdir(parents=True)
        (image_dir / "abc.png").write_bytes(b"image")
        _write_image_mapping(output_dir, image_url, "images_unique/abc.png")
        ThreadArchiveStore(thread_dir).floor_maps.replace_floor_map(
            StoredFloorMap(
                version=FLOOR_MAP_VERSION,
                generation_version=FLOOR_MAP_GENERATION_VERSION,
                algorithm=FLOOR_MAP_HASH_ALGORITHM,
                tid=101,
                aid=201,
                input_signature="fixture",
                entries=[{"pid": 1, "author_lou": 1, "original_lou": 5}],
            )
        )
        legacy_floor_map = thread_dir / "floor_map.json"
        legacy_floor_map.write_text("{bad", encoding="utf-8")

        result = read_posts(
            output_dir,
            101,
            "201",
            page=1,
            query="hello",
        )

        assert result["matchingPostCount"] == 1
        assert result["items"][0]["authorName"] == "author-1"
        assert result["items"][0]["floorLabel"] == "第1楼（原5楼）"
        assert 'src="/api/files/images_unique/abc.png"' in result["items"][0]["html"]
        assert [slot["lou"] for slot in result["slots"]] == [1]
        assert all(slot["emptyReason"] is None for slot in result["slots"])
        assert legacy_floor_map.read_text(encoding="utf-8") == "{bad"

    def test_current_and_historical_posts_do_not_recover_attachment_urls(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        relative_src = "./mon_202607/08/attachment.png"
        image_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/08/attachment.png"
        )
        old_post = _post(1, f"old [img]{relative_src}[/img]")
        old_post["attches"] = [
            {"type": "img", "attachurl": "mon_202607/08/attachment.png"}
        ]
        new_post = _post(1, f"new [img]{relative_src}[/img]")
        new_post["attches"] = [
            {"type": "img", "attachurl": "mon_202607/08/attachment.png"}
        ]
        _write_archive(thread_dir, [old_post])
        _write_archive(thread_dir, [new_post])
        image_path = output_dir / "images_unique" / "attachment.png"
        image_path.parent.mkdir(parents=True)
        Image.new("RGB", (2, 2), color="white").save(image_path)
        _write_image_mapping(
            output_dir,
            image_url,
            "images_unique/attachment.png",
        )
        with closing(sqlite3.connect(thread_dir / "archive.sqlite3")) as connection:
            old_version_id = connection.execute(
                "SELECT id FROM post_versions WHERE source_hash = ?",
                (hash_text(f"old [img]{relative_src}[/img]"),),
            ).fetchone()[0]

        current_html = read_posts(
            output_dir,
            101,
            "201",
            page=1,
        )["items"][0]["html"]
        historical_html = read_post_version_preview(
            output_dir,
            101,
            "201",
            old_version_id,
        )["item"]["html"]

        assert f"[img]{relative_src}[/img]" in current_html
        assert f"[img]{relative_src}[/img]" in historical_html
        assert "<img" not in current_html
        assert "<img" not in historical_html

    def test_current_and_historical_posts_decode_numeric_entities(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "old&amp;#160;decimal")])
        _write_archive(
            thread_dir,
            [_post(1, "new&amp;#xA0;hex")],
        )
        with closing(sqlite3.connect(thread_dir / "archive.sqlite3")) as connection:
            old_version_id = connection.execute(
                "SELECT id FROM post_versions WHERE source_hash = ?",
                (hash_text("old&amp;#160;decimal"),),
            ).fetchone()[0]

        current_html = read_posts(output_dir, 101, "201", page=1)["items"][0][
            "html"
        ]
        historical_html = read_post_version_preview(
            output_dir,
            101,
            "201",
            old_version_id,
        )["item"]["html"]

        assert current_html == "new&nbsp;hex"
        assert historical_html == "old&nbsp;decimal"

    def test_reads_posts_sanitizes_untrusted_html(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        image_url = "https://img.nga.178.com/attachments/mon_202607/08/abc.png"
        _write_archive(
            thread_dir,
            [
                _post(
                    1,
                    (
                        '<p onclick="alert(1)">hello</p>'
                        '<script>alert("xss")</script>'
                        '<svg><animate onbegin="alert(1)" /></svg>'
                        f'[img]{image_url}[/img]'
                        '<a href="javascript:alert(3)" onclick="alert(4)">bad</a>'
                        '<a href="&amp;#106;avascript:alert(5)">encoded bad</a>'
                        '<span style="color:red;position:fixed">styled</span>'
                    ),
                )
            ],
        )
        image_dir = output_dir / "images_unique"
        image_dir.mkdir(parents=True)
        (image_dir / "abc.png").write_bytes(b"image")
        _write_image_mapping(output_dir, image_url, "images_unique/abc.png")

        result = read_posts(output_dir, 101, "201", page=1)
        html = result["items"][0]["html"]
        lowered_html = html.lower()

        assert 'src="/api/files/images_unique/abc.png"' in html
        assert "onclick" not in lowered_html
        assert "onerror" not in lowered_html
        assert "<script" not in lowered_html
        assert "<svg" not in lowered_html
        assert "javascript:" not in lowered_html
        assert "position" not in lowered_html

    def test_reads_posts_rewrites_downloaded_audio_as_safe_local_player(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        audio_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/15/thread-bgm.mp3"
        )
        _write_archive(
            thread_dir,
            [
                _post(
                    1,
                    (
                        f'<audio src="{audio_url}" autoplay loop '
                        'onplay="alert(1)"><source src="https://example.com/evil.mp3">'
                        "fallback</audio>"
                    ),
                )
            ],
        )
        audio_path = _write_audio_mapping(output_dir, audio_url)

        result = read_posts(output_dir, 101, "201", page=1)
        html = result["items"][0]["html"]

        expected_src = f'/api/files/audio_unique/{audio_path.name}'
        assert "<audio " in html
        assert 'class="nga-audio-player"' in html
        assert f'src="{expected_src}"' in html
        assert 'preload="none"' in html
        assert "controls" in html
        assert audio_url not in html
        assert "autoplay" not in html
        assert "onplay" not in html
        assert "<source" not in html

    def test_reads_posts_never_falls_back_to_missing_or_corrupt_remote_audio(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        missing_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/15/missing-bgm.mp3"
        )
        corrupt_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/15/corrupt-bgm.mp3"
        )
        _write_archive(
            thread_dir,
            [
                _post(1, f'<audio src="{missing_url}"></audio>'),
                _post(2, f'<audio src="{corrupt_url}"></audio>'),
            ],
        )
        corrupt_path = _write_audio_mapping(output_dir, corrupt_url)
        corrupt_path.write_bytes(b"x" * corrupt_path.stat().st_size)

        result = read_posts(output_dir, 101, "201", page=1)
        html_by_lou = {item["lou"]: item["html"] for item in result["items"]}

        for lou, remote_url in ((1, missing_url), (2, corrupt_url)):
            assert "音频未下载或不可用" in html_by_lou[lou]
            assert "<audio" not in html_by_lou[lou]
            assert remote_url not in html_by_lou[lou]

    def test_historical_post_version_uses_downloaded_audio_mapping(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        audio_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/15/historical-bgm.mp3"
        )
        _write_archive(thread_dir, [_post(1, f'<audio src="{audio_url}"></audio>')])
        _write_archive(thread_dir, [_post(1, "current version")])
        audio_path = _write_audio_mapping(output_dir, audio_url)
        with closing(sqlite3.connect(thread_dir / "archive.sqlite3")) as connection:
            historical_version_id = connection.execute(
                "SELECT id FROM post_versions WHERE source_hash = ?",
                (hash_text(f'<audio src="{audio_url}"></audio>'),),
            ).fetchone()[0]

        result = read_post_version_preview(
            output_dir,
            101,
            "201",
            historical_version_id,
        )
        html = result["item"]["html"]

        assert f'src="/api/files/audio_unique/{audio_path.name}"' in html
        assert audio_url not in html

    def test_renders_web_only_bbcode_collapse_and_quote(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(
            thread_dir,
            [_post(1, "[quote]引用[/quote][collapse=标题][h]章节[/h]正文[/collapse]")],
        )

        result = read_posts(output_dir, 101, "201", page=1)
        html = result["items"][0]["html"]

        assert '<blockquote class="nga-quote">引用</blockquote>' in html
        assert '<details class="nga-collapse">' in html
        assert "<summary>标题</summary>" in html
        assert '<h4 class="nga-bbcode-heading">章节</h4>' in html

    def test_reads_posts_preserves_string_postdate(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        post = _post(1, "hello")
        post["postdate"] = "2025-06-17 13:42"
        _write_archive(thread_dir, [post])

        result = read_posts(output_dir, 101, "201", page=1)

        assert result["items"][0]["postdate"] == "2025-06-17 13:42"

    def test_safe_output_file_rejects_paths_outside_output_dir(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        allowed = output_dir / "images_unique" / "abc.png"
        allowed.parent.mkdir()
        allowed.write_bytes(b"image")
        outside = tmp_path / "secret.txt"
        outside.write_text("secret", encoding="utf-8")

        assert safe_output_file(output_dir, "images_unique/abc.png") == allowed
        assert safe_output_file(output_dir, "../secret.txt") is None
