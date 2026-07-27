from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from concurrent.futures import Future
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from nga_tools.backup.image_index_writer import (
    ImageMappingRow,
    active_image_index_writer,
)
from nga_tools.backup.image_store_metrics import (
    record_image_mapping_failure,
    record_image_mapping_submission,
    time_image_store_phase,
)
from nga_tools.core.image_formats import image_file_is_valid
from nga_tools.core.nga_images import NGA_img_link_verify
from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
    configure_readonly_connection,
    iter_in_clause_chunks,
)
from nga_tools.storage import (
    UnsupportedStorageFormatError,
    ensure_storage_metadata,
    require_storage_metadata,
)
from nga_tools.storage.schema import require_exact_columns, require_table_names

IMAGE_INDEX_FILENAME = "image_index.sqlite3"
_IMAGE_MAPPINGS_COLUMNS = (("url", "TEXT"), ("unique_rel_path", "TEXT"))
_IMAGE_INDEX_LOCK = threading.RLock()
_INITIALIZED_IMAGE_INDEX_PATHS: set[Path] = set()


def normalize_nga_image_url(url: str) -> str:
    return url.replace(",", "")


def _normalized_nga_image_urls(urls: Iterable[str]) -> list[str]:
    return sorted(
        {
            normalized_url
            for url in urls
            if NGA_img_link_verify(
                normalized_url := normalize_nga_image_url(url)
            )
        }
    )


@dataclass(frozen=True)
class ImageMapping:
    output_root: Path
    url: str
    unique_rel_path: str

    @property
    def unique_path(self) -> Path:
        return self.output_root / self.unique_rel_path


def require_current_image_index(
    connection: sqlite3.Connection,
    db_path: Path,
) -> None:
    source = f"image_index {db_path}"
    require_storage_metadata(connection, role="image_index")
    require_table_names(
        connection,
        expected={"storage_metadata", "image_mappings"},
        source=source,
    )
    require_exact_columns(
        connection,
        "image_mappings",
        _IMAGE_MAPPINGS_COLUMNS,
        source=source,
    )


class ImageIndexStore:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root.resolve()
        self.db_path = self.output_root / IMAGE_INDEX_FILENAME

    def _initialize(self) -> Path:
        db_path = self.db_path
        with _IMAGE_INDEX_LOCK:
            if db_path in _INITIALIZED_IMAGE_INDEX_PATHS and db_path.is_file():
                return db_path

            new_database = not db_path.is_file()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(
                sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
            ) as connection:
                configure_connection(connection)
                with connection:
                    if new_database:
                        ensure_storage_metadata(connection, role="image_index")
                    else:
                        require_storage_metadata(connection, role="image_index")
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS image_mappings (
                            url TEXT PRIMARY KEY,
                            unique_rel_path TEXT NOT NULL
                        )
                        """
                    )
                    require_current_image_index(connection, db_path)
            _INITIALIZED_IMAGE_INDEX_PATHS.add(db_path)
        return db_path

    def open_writable_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._initialize(),
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        configure_connection(connection)
        return connection

    def open_readonly_connection(self) -> sqlite3.Connection:
        db_path = self.db_path
        if not db_path.is_file():
            raise FileNotFoundError(db_path)
        connection = sqlite3.connect(
            f"{db_path.as_uri()}?mode=ro",
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
            uri=True,
        )
        configure_readonly_connection(connection)
        try:
            require_current_image_index(connection, db_path)
        except BaseException:
            connection.close()
            raise
        return connection

    def _mapping(self, url: str, unique_rel_path: str) -> ImageMapping:
        return ImageMapping(self.output_root, url, unique_rel_path)

    def _unique_rel_path(self, path: Path) -> str:
        return os.path.relpath(path, self.output_root).replace("\\", "/")

    def enqueue_mappings(
        self,
        mappings: list[tuple[str, Path]],
    ) -> tuple[list[ImageMapping], Future[None]]:
        if not mappings:
            future = Future[None]()
            future.set_result(None)
            return [], future

        record_image_mapping_submission(len(mappings))
        with time_image_store_phase("mapping_submit"):
            image_mappings = [
                self._mapping(url, self._unique_rel_path(unique_path))
                for url, unique_path in mappings
            ]
            rows: list[ImageMappingRow] = [
                (mapping.url, mapping.unique_rel_path)
                for mapping in image_mappings
            ]
            writer = active_image_index_writer(self._initialize())
            if writer is not None:
                return image_mappings, writer.submit(rows)

            future = Future[None]()
            with _IMAGE_INDEX_LOCK:
                try:
                    with closing(self.open_writable_connection()) as connection:
                        with connection:
                            connection.executemany(
                                """
                                INSERT INTO image_mappings (url, unique_rel_path)
                                VALUES (?, ?)
                                ON CONFLICT(url) DO UPDATE SET
                                    unique_rel_path = excluded.unique_rel_path
                                """,
                                rows,
                            )
                except BaseException as error:
                    future.set_exception(error)
                else:
                    future.set_result(None)
            return image_mappings, future

    def upsert_mappings(
        self,
        mappings: list[tuple[str, Path]],
    ) -> list[ImageMapping]:
        image_mappings, future = self.enqueue_mappings(mappings)
        self.wait_for_mapping(future)
        return image_mappings

    def wait_for_mapping(self, future: Future[None]) -> None:
        with time_image_store_phase("mapping_wait"):
            try:
                future.result()
            except BaseException:
                record_image_mapping_failure()
                raise

    def upsert_mapping(self, url: str, unique_path: Path) -> ImageMapping:
        return self.upsert_mappings([(url, unique_path)])[0]

    def mapping_for_url(self, url: str) -> ImageMapping | None:
        normalized_url = normalize_nga_image_url(url)
        if not NGA_img_link_verify(normalized_url) or not self.db_path.is_file():
            return None
        with closing(self.open_readonly_connection()) as connection:
            row = connection.execute(
                "SELECT unique_rel_path FROM image_mappings WHERE url = ?",
                (normalized_url,),
            ).fetchone()
        if row is None or not isinstance(row[0], str):
            return None
        return self._mapping(normalized_url, row[0])

    def mappings_by_url(self) -> dict[str, ImageMapping]:
        if not self.db_path.is_file():
            return {}
        with closing(self.open_readonly_connection()) as connection:
            rows = connection.execute(
                "SELECT url, unique_rel_path FROM image_mappings"
            ).fetchall()
        return {
            url: self._mapping(url, unique_rel_path)
            for url, unique_rel_path in rows
            if isinstance(url, str) and isinstance(unique_rel_path, str)
        }

    def mappings_for_urls(
        self,
        urls: Iterable[str],
    ) -> dict[str, ImageMapping]:
        normalized_urls = _normalized_nga_image_urls(urls)
        if not normalized_urls or not self.db_path.is_file():
            return {}
        with closing(self.open_readonly_connection()) as connection:
            return self._mappings_for_normalized_urls_in_connection(
                connection,
                normalized_urls,
            )

    def mappings_for_urls_in_connection(
        self,
        connection: sqlite3.Connection,
        urls: Iterable[str],
    ) -> dict[str, ImageMapping]:
        return self._mappings_for_normalized_urls_in_connection(
            connection,
            _normalized_nga_image_urls(urls),
        )

    def _mappings_for_normalized_urls_in_connection(
        self,
        connection: sqlite3.Connection,
        normalized_urls: list[str],
    ) -> dict[str, ImageMapping]:
        mappings: dict[str, ImageMapping] = {}
        for chunk in iter_in_clause_chunks(normalized_urls):
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT url, unique_rel_path
                FROM image_mappings
                WHERE url IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            for url, unique_rel_path in rows:
                if isinstance(url, str) and isinstance(unique_rel_path, str):
                    mappings[url] = self._mapping(url, unique_rel_path)
        return mappings

    def iter_mapping_rows(
        self,
        connection: sqlite3.Connection,
        *,
        batch_size: int = 10_000,
        order_by_url: bool = False,
    ) -> Iterator[tuple[str, str]]:
        for url, unique_rel_path in self.iter_raw_mapping_rows(
            connection,
            batch_size=batch_size,
            order_by_url=order_by_url,
        ):
            if isinstance(url, str) and isinstance(unique_rel_path, str):
                yield url, unique_rel_path

    def iter_raw_mapping_rows(
        self,
        connection: sqlite3.Connection,
        *,
        batch_size: int = 10_000,
        order_by_url: bool = False,
    ) -> Iterator[tuple[object, object]]:
        sql = "SELECT url, unique_rel_path FROM image_mappings"
        if order_by_url:
            sql += " ORDER BY url"
        cursor = connection.execute(sql)
        while rows := cursor.fetchmany(batch_size):
            for url, unique_rel_path in rows:
                yield url, unique_rel_path

    def existing_paths_for_urls(self, urls: Iterable[str]) -> dict[str, Path]:
        try:
            mappings = self.mappings_for_urls(urls)
        except UnsupportedStorageFormatError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            return {}
        images_root = (self.output_root / "images_unique").resolve()
        paths_by_url: dict[str, Path] = {}
        for url, mapping in mappings.items():
            relative_path = Path(mapping.unique_rel_path)
            if (
                relative_path.is_absolute()
                or not relative_path.parts
                or relative_path.parts[0] != "images_unique"
            ):
                continue
            image_path = mapping.unique_path.resolve()
            if not image_path.is_relative_to(images_root):
                continue
            if not image_file_is_valid(image_path):
                continue
            paths_by_url[url] = image_path
        return paths_by_url
