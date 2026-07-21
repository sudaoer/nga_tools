from __future__ import annotations

from typing import cast

from nga_tools.backup.archive_repository import ArchiveRepository
from nga_tools.backup.post_overlay import (
    PostOverlay,
    post_overlay_from_storage,
    post_overlays_fingerprint,
)


class ArchiveOverlayRepository(ArchiveRepository):
    @staticmethod
    def _post_overlays_from_rows(
        rows: list[tuple[object, object, object, object, object]],
    ) -> dict[int, PostOverlay]:
        overlays: dict[int, PostOverlay] = {}
        for row in rows:
            lou, mode, bbcode, content_hash, updated_at = row
            if type(lou) is not int or lou < 0:
                raise ValueError(f"archive post overlay楼层无效：{lou!r}")
            try:
                overlays[lou] = post_overlay_from_storage(
                    mode=mode,
                    bbcode=bbcode,
                    content_hash=content_hash,
                    updated_at=updated_at,
                )
            except ValueError as error:
                raise ValueError(
                    f"archive第{lou}楼post overlay无效：{error}"
                ) from error
        return overlays

    def read_post_overlays(
        self,
        lous: set[int] | None = None,
    ) -> dict[int, PostOverlay]:
        if not self.exists() or (lous is not None and not lous):
            return {}

        params: tuple[int, ...] = ()
        where_lous = ""
        if lous is not None:
            sorted_lous = tuple(sorted(lous))
            placeholders = ",".join("?" for _ in sorted_lous)
            where_lous = f"WHERE lou IN ({placeholders})"
            params = sorted_lous

        with self._read_connection() as connection:
            rows = cast(
                list[tuple[object, object, object, object, object]],
                connection.execute(
                    f"""
                    SELECT lou, mode, bbcode, content_hash, updated_at
                    FROM post_overlays
                    {where_lous}
                    ORDER BY lou
                    """,
                    params,
                ).fetchall(),
            )
        return self._post_overlays_from_rows(rows)

    def upsert_post_overlay(self, lou: int, overlay: PostOverlay) -> PostOverlay:
        if type(lou) is not int or lou < 0:
            raise ValueError(f"overlay楼层必须是非负整数：{lou!r}")
        normalized_overlay = post_overlay_from_storage(
            mode=overlay["mode"],
            bbcode=overlay["bbcode"],
            content_hash=overlay["content_hash"],
            updated_at=overlay["updated_at"],
        )
        with self._write_connection() as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO post_overlays (
                        lou,
                        mode,
                        bbcode,
                        content_hash,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(lou) DO UPDATE SET
                        mode = excluded.mode,
                        bbcode = excluded.bbcode,
                        content_hash = excluded.content_hash,
                        updated_at = excluded.updated_at
                    """,
                    (
                        lou,
                        normalized_overlay["mode"],
                        normalized_overlay["bbcode"],
                        normalized_overlay["content_hash"],
                        normalized_overlay["updated_at"],
                    ),
                )
        return normalized_overlay

    def delete_post_overlay(self, lou: int) -> bool:
        if type(lou) is not int or lou < 0:
            raise ValueError(f"overlay楼层必须是非负整数：{lou!r}")
        with self._write_connection() as connection:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM post_overlays WHERE lou = ?",
                    (lou,),
                )
        return cursor.rowcount > 0

    def post_overlays_fingerprint(self) -> str:
        return post_overlays_fingerprint(self.read_post_overlays())
