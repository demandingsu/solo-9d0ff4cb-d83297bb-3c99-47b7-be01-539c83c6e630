"""SQLite persistence with all-or-nothing version writes.

Each version row and its attachment blob are inserted in the same explicit
transaction. sqlite3's isolation_level is disabled so we control BEGIN/COMMIT
and never leave autocommitted fragments behind. A write-ahead log plus
synchronous=FULL makes an interrupted process leave either a complete version
or no version at all (SQLite rolls the partial transaction back on recovery).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable

from .domain import (
    ArchiveStorageError,
    VersionRecord,
    utc_now_iso,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    number          TEXT PRIMARY KEY,
    first_seen_at   TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    source_retracted INTEGER NOT NULL DEFAULT 0
                      CHECK (source_retracted IN (0, 1)),
    status_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS versions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_number     TEXT NOT NULL
                            REFERENCES announcements(number),
    revision                INTEGER NOT NULL,
    body_revision           INTEGER NOT NULL,
    attachment_revision     INTEGER NOT NULL,
    title                   TEXT NOT NULL,
    body                    TEXT NOT NULL,
    body_sha256             TEXT NOT NULL,
    content_hash            TEXT NOT NULL,
    attachment_filename     TEXT NOT NULL,
    attachment_content_type TEXT NOT NULL,
    attachment_size         INTEGER NOT NULL,
    attachment_sha256       TEXT NOT NULL,
    attachment_blob         BLOB NOT NULL,
    source_retracted        INTEGER NOT NULL CHECK (source_retracted IN (0, 1)),
    source_url              TEXT NOT NULL,
    source_updated_at       TEXT,
    captured_at             TEXT NOT NULL,
    UNIQUE (announcement_number, revision)
);

CREATE INDEX IF NOT EXISTS versions_number_captured
    ON versions (announcement_number, revision DESC);

CREATE TABLE IF NOT EXISTS capture_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_number TEXT,
    outcome         TEXT NOT NULL,
    revision        INTEGER,
    detail          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


class ArchiveStore:
    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA)
            # Additive migration for databases created by earlier builds.
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(announcements)")
            }
            if "source_retracted" not in columns:
                conn.execute(
                    "ALTER TABLE announcements ADD COLUMN "
                    "source_retracted INTEGER NOT NULL DEFAULT 0"
                )
            if "status_updated_at" not in columns:
                conn.execute(
                    "ALTER TABLE announcements ADD COLUMN status_updated_at TEXT"
                )
        except sqlite3.Error as exc:
            raise ArchiveStorageError(f"无法初始化档案数据库：{exc}") from exc
        finally:
            conn.close()

    # ---------------------------------------------------------------- reads

    def get_version(
        self, number: str, revision: int
    ) -> VersionRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM versions WHERE announcement_number = ? "
                "AND revision = ?",
                (number, revision),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_record(row) if row else None

    def list_versions(self, number: str) -> list[VersionRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM versions WHERE announcement_number = ? "
                "ORDER BY revision DESC",
                (number,),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_record(r) for r in rows]

    def latest_version(self, number: str) -> VersionRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM versions WHERE announcement_number = ? "
                "ORDER BY revision DESC LIMIT 1",
                (number,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_record(row) if row else None

    def announcement_exists(self, number: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM announcements WHERE number = ?", (number,)
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def list_announcements(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT a.number, a.first_seen_at, a.last_checked_at,
                       a.source_retracted, a.status_updated_at,
                       COUNT(v.id) AS version_count,
                       (
                           SELECT v2.revision FROM versions v2
                           WHERE v2.announcement_number = a.number
                           ORDER BY v2.revision DESC LIMIT 1
                       ) AS latest_revision,
                       (
                           SELECT v3.title FROM versions v3
                           WHERE v3.announcement_number = a.number
                           ORDER BY v3.revision DESC LIMIT 1
                       ) AS latest_title
                FROM announcements a
                LEFT JOIN versions v ON v.announcement_number = a.number
                GROUP BY a.number
                ORDER BY a.last_checked_at DESC, a.number
                """
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def get_announcement(self, number: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM announcements WHERE number = ?", (number,)
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def recent_events(self, limit: int = 30) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM capture_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def events_for(self, number: str, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM capture_log WHERE announcement_number = ? "
                "ORDER BY id DESC LIMIT ?",
                (number, limit),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- writes

    def insert_version(
        self, record: VersionRecord, outcome: str, detail: str
    ) -> bool:
        """Atomically archive one version.

        Returns False when that source revision was already archived (no new
        version row is created). Raises ArchiveStorageError on any storage
        failure, rolling back so no partial version can be observed.
        """
        conn = self._connect()
        now = record.captured_at or utc_now_iso()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM versions WHERE announcement_number = ? "
                "AND revision = ?",
                (record.announcement_number, record.revision),
            ).fetchone()
            if existing:
                conn.execute("ROLLBACK")
                return False

            conn.execute(
                """
                INSERT INTO announcements
                    (number, first_seen_at, last_checked_at,
                     source_retracted, status_updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(number) DO UPDATE SET
                    last_checked_at = excluded.last_checked_at,
                    source_retracted = excluded.source_retracted,
                    status_updated_at = excluded.status_updated_at
                """,
                (
                    record.announcement_number,
                    now,
                    now,
                    int(record.source_retracted),
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO versions (
                    announcement_number, revision, body_revision,
                    attachment_revision, title, body, body_sha256,
                    content_hash, attachment_filename,
                    attachment_content_type, attachment_size,
                    attachment_sha256, attachment_blob, source_retracted,
                    source_url, source_updated_at, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.announcement_number,
                    record.revision,
                    record.body_revision,
                    record.attachment_revision,
                    record.title,
                    record.body,
                    record.body_sha256,
                    record.content_hash,
                    record.attachment_filename,
                    record.attachment_content_type,
                    record.attachment_size,
                    record.attachment_sha256,
                    sqlite3.Binary(record.attachment_blob),
                    int(record.source_retracted),
                    record.source_url,
                    record.source_updated_at,
                    record.captured_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO capture_log
                    (announcement_number, outcome, revision, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.announcement_number,
                    outcome,
                    record.revision,
                    detail,
                    now,
                ),
            )
            conn.execute("COMMIT")
            return True
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise ArchiveStorageError(f"存档写入失败，未保存任何内容：{exc}") from exc
        finally:
            conn.close()

    def touch_checked(
        self, number: str, outcome: str, detail: str, revision: int | None
    ) -> None:
        """Record a capture attempt that did not create a version."""
        now = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO capture_log
                    (announcement_number, outcome, revision, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (number, outcome, revision, detail, now),
            )
            conn.execute(
                "UPDATE announcements SET last_checked_at = ? WHERE number = ?",
                (now, number),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise ArchiveStorageError(f"无法记录采集结果：{exc}") from exc
        finally:
            conn.close()

    def update_source_retracted(
        self, number: str, revision: int, retracted: bool
    ) -> None:
        """Record the site's retraction state without altering any version.

        Version rows (text + attachment bytes) are immutable after capture.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE announcements SET source_retracted = ?, "
                "status_updated_at = ?, last_checked_at = ? WHERE number = ?",
                (int(retracted), utc_now_iso(), utc_now_iso(), number),
            )
            if cursor.rowcount != 1:
                raise ArchiveStorageError("撤稿状态更新失败：目标公告不存在。")
            conn.execute(
                """
                INSERT INTO capture_log
                    (announcement_number, outcome, revision, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    number,
                    "retracted_update",
                    revision,
                    "源站撤稿状态变化，已更新标记" if retracted else "源站恢复发布，已更新标记",
                    utc_now_iso(),
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise ArchiveStorageError(f"无法更新撤稿状态：{exc}") from exc
        finally:
            conn.close()

    def log_event(
        self, number: str | None, outcome: str, detail: str, revision: int | None
    ) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO capture_log
                    (announcement_number, outcome, revision, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (number, outcome, revision, detail, utc_now_iso()),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise ArchiveStorageError(f"无法记录采集事件：{exc}") from exc
        finally:
            conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> VersionRecord:
        blob: Iterable[int] | bytes = row["attachment_blob"]
        return VersionRecord(
            announcement_number=row["announcement_number"],
            revision=row["revision"],
            body_revision=row["body_revision"],
            attachment_revision=row["attachment_revision"],
            title=row["title"],
            body=row["body"],
            body_sha256=row["body_sha256"],
            content_hash=row["content_hash"],
            attachment_filename=row["attachment_filename"],
            attachment_content_type=row["attachment_content_type"],
            attachment_size=row["attachment_size"],
            attachment_sha256=row["attachment_sha256"],
            attachment_blob=bytes(blob),
            source_retracted=bool(row["source_retracted"]),
            source_url=row["source_url"],
            source_updated_at=row["source_updated_at"],
            captured_at=row["captured_at"],
        )
