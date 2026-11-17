"""SQLite persistence with all-or-nothing version writes.

Each version row, its attachment blob and its search-index rows are inserted
in the same explicit transaction. sqlite3's isolation_level is disabled so we
control BEGIN/COMMIT and never leave autocommitted fragments behind. A
write-ahead log plus synchronous=FULL makes an interrupted process leave
either a complete version or no version at all (SQLite rolls the partial
transaction back on recovery).

Search index: ``search_docs`` holds the indexed text (title + body, never the
attachment blob) and ``search_grams`` a trigram postings table, so literal,
case-sensitive queries are answered from the index alone — the versions
table and attachment blobs are never scanned at query time. ``search_state``
tracks backfill progress: historical versions are indexed in chunked
transactions and the index is marked 'ready' only inside the transaction
that verifies no unindexed version remains, so an interrupted or failed
backfill can never serve partial results.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Iterable

from .domain import (
    ArchiveStorageError,
    SearchHit,
    SearchNotReady,
    VersionRecord,
    snippet_parts,
    trigrams,
    utc_now_iso,
)

log = logging.getLogger(__name__)

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

-- Search index: one row per archived version holding the searchable text
-- (title + body only; attachment bytes are never copied here).
CREATE TABLE IF NOT EXISTS search_docs (
    version_id          INTEGER PRIMARY KEY
                        REFERENCES versions(id),
    announcement_number TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    title               TEXT NOT NULL,
    body                TEXT NOT NULL
);

-- Trigram postings: version_id lists keyed by 3-character substring.
CREATE TABLE IF NOT EXISTS search_grams (
    gram        TEXT NOT NULL,
    version_id  INTEGER NOT NULL,
    PRIMARY KEY (gram, version_id)
) WITHOUT ROWID;

-- Backfill bookkeeping. Exactly one row (id = 1).
CREATE TABLE IF NOT EXISTS search_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    status          TEXT NOT NULL CHECK (status IN ('building', 'ready', 'failed')),
    indexed_up_to   INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    detail          TEXT
);
"""

_UNINDEXED_SQL = """
SELECT v.id, v.announcement_number, v.revision, v.title, v.body
FROM versions v
WHERE NOT EXISTS (SELECT 1 FROM search_docs d WHERE d.version_id = v.id)
ORDER BY v.id
"""

# A query contributes at most this many distinct trigrams to the narrowing
# phase; the instr() verification keeps results exact regardless.
SEARCH_GRAM_CAP = 64


class ArchiveStore:
    def __init__(self, db_path: str | os.PathLike[str], *, auto_index: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()
        if auto_index:
            self._auto_backfill()

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

    # --------------------------------------------------------------- search

    def search_status(self) -> dict:
        """Readiness of the text index for the search page.

        'ready' means every committed version is indexed; anything else means
        search must refuse to answer rather than show incomplete results.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT status, detail, updated_at FROM search_state WHERE id = 1"
            ).fetchone()
            status = row["status"] if row else "building"
            result = {
                "ready": status == "ready",
                "status": status,
                "detail": (row["detail"] if row else None) or None,
                "updated_at": row["updated_at"] if row else None,
            }
            if status != "ready":
                result["indexed"] = conn.execute(
                    "SELECT COUNT(*) FROM search_docs"
                ).fetchone()[0]
                result["total"] = conn.execute(
                    "SELECT COUNT(*) FROM versions"
                ).fetchone()[0]
                if row is None:
                    result["detail"] = "检索索引尚未建立。"
            return result
        finally:
            conn.close()

    def search_versions(
        self, query: str, *, limit: int = 200
    ) -> tuple[list[SearchHit], bool]:
        """Literal, case-sensitive search over indexed titles and bodies.

        Reads only the search-index tables (never the versions table or
        attachment blobs) inside one read transaction, so the result list is
        a single committed snapshot and every hit references a committed
        revision. Raises SearchNotReady if the index is incomplete.
        """
        grams = sorted(trigrams(query))[:SEARCH_GRAM_CAP]
        if not grams:
            return [], False
        placeholders = ", ".join("?" for _ in grams)
        sql = f"""
            SELECT d.announcement_number, d.revision, d.title, d.body
            FROM search_docs d
            WHERE d.version_id IN (
                SELECT g.version_id FROM search_grams g
                WHERE g.gram IN ({placeholders})
                GROUP BY g.version_id
                HAVING COUNT(DISTINCT g.gram) = ?
            )
            AND (instr(d.title, ?) > 0 OR instr(d.body, ?) > 0)
            ORDER BY d.announcement_number, d.revision DESC
            LIMIT ?
        """
        params = (*grams, len(grams), query, query, limit + 1)
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT status FROM search_state WHERE id = 1"
            ).fetchone()
            if row is None or row["status"] != "ready":
                conn.execute("ROLLBACK")
                raise SearchNotReady(
                    "检索索引未就绪：历史版本补建未完成或曾失败，"
                    "为避免展示不完整结果，本次不返回命中。"
                )
            rows = conn.execute(sql, params).fetchall()
            conn.execute("COMMIT")
        except SearchNotReady:
            raise
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise ArchiveStorageError(f"检索查询失败：{exc}") from exc
        finally:
            conn.close()

        truncated = len(rows) > limit
        hits: list[SearchHit] = []
        for row in rows[:limit]:
            snippet = snippet_parts(row["title"], query)
            field = "title"
            if snippet is None:
                snippet = snippet_parts(row["body"], query)
                field = "body"
            if snippet is None:
                continue  # instr() verified a match; never emit a false hit
            before, hit, after = snippet
            hits.append(
                SearchHit(
                    announcement_number=row["announcement_number"],
                    revision=row["revision"],
                    field=field,
                    before=before,
                    hit=hit,
                    after=after,
                )
            )
        return hits, truncated

    def backfill_search_index(
        self, *, chunk_size: int = 200, max_chunks: int | None = None
    ) -> None:
        """Index every archived version missing from the search index.

        Each chunk commits separately, so an interrupted run leaves resumable
        progress with the state stuck at 'building' (never silently partial).
        The 'ready' mark is written only in the final transaction, after
        verifying that no unindexed version remains. Versions captured while
        the backfill runs are indexed by insert_version itself and skipped
        here. ``max_chunks`` bounds the work done in one call (used to
        simulate an interrupted backfill); remaining versions stay pending.
        """
        if chunk_size < 1:
            raise ValueError("chunk_size 必须为正整数")
        conn = self._connect()
        chunks = 0
        try:
            row = conn.execute(
                "SELECT status FROM search_state WHERE id = 1"
            ).fetchone()
            if row is not None and row["status"] == "ready":
                missing = conn.execute(_UNINDEXED_SQL + " LIMIT 1").fetchone()
                if missing is None:
                    return  # already complete; nothing to do
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO search_state
                    (id, status, indexed_up_to, updated_at, detail)
                VALUES (1, 'building', 0, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    status = 'building',
                    updated_at = excluded.updated_at,
                    detail = NULL
                """,
                (utc_now_iso(),),
            )
            conn.execute("COMMIT")

            while True:
                if max_chunks is not None and chunks >= max_chunks:
                    return
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    _UNINDEXED_SQL + " LIMIT ?", (chunk_size,)
                ).fetchall()
                if not rows:
                    conn.execute(
                        "UPDATE search_state SET status = 'ready', "
                        "updated_at = ?, detail = NULL WHERE id = 1",
                        (utc_now_iso(),),
                    )
                    conn.execute("COMMIT")
                    return
                for row in rows:
                    self._index_version_tx(
                        conn,
                        row["id"],
                        row["announcement_number"],
                        row["revision"],
                        row["title"],
                        row["body"],
                    )
                conn.execute(
                    "UPDATE search_state SET indexed_up_to = ?, "
                    "updated_at = ? WHERE id = 1",
                    (rows[-1]["id"], utc_now_iso()),
                )
                conn.execute("COMMIT")
                chunks += 1
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self._mark_search_failed(f"检索索引补建失败：{exc}")
            raise ArchiveStorageError(
                f"检索索引补建失败：{exc}；采集与原档浏览不受影响。"
            ) from exc
        finally:
            conn.close()

    def _auto_backfill(self) -> None:
        """First enablement on an old database: index historical revisions.

        A backfill failure must never take capture or browsing down, so it is
        recorded in the index state (search then reports 'not ready') instead
        of being raised to the caller.
        """
        try:
            self.backfill_search_index()
        except ArchiveStorageError:
            log.exception(
                "检索索引补建失败；检索将显示未就绪，采集与原档浏览不受影响"
            )

    def _mark_search_failed(self, detail: str) -> None:
        try:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO search_state
                        (id, status, indexed_up_to, updated_at, detail)
                    VALUES (1, 'failed', 0, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status = 'failed',
                        updated_at = excluded.updated_at,
                        detail = excluded.detail
                    """,
                    (utc_now_iso(), detail),
                )
                conn.execute("COMMIT")
            finally:
                conn.close()
        except sqlite3.Error:
            log.exception("无法记录检索索引失败状态")

    def _index_version_tx(
        self,
        conn: sqlite3.Connection,
        version_id: int,
        number: str,
        revision: int,
        title: str,
        body: str,
    ) -> None:
        """Index one version inside the caller's transaction.

        Runs in the same BEGIN IMMEDIATE … COMMIT as the version insert (or
        as one backfill chunk), so a committed version is always searchable
        and a failed index write rolls the version back with it.
        """
        conn.execute(
            """
            INSERT INTO search_docs
                (version_id, announcement_number, revision, title, body)
            VALUES (?, ?, ?, ?, ?)
            """,
            (version_id, number, revision, title, body),
        )
        grams = trigrams(title) | trigrams(body)
        if grams:
            conn.executemany(
                "INSERT INTO search_grams (gram, version_id) VALUES (?, ?)",
                [(gram, version_id) for gram in grams],
            )

    # --------------------------------------------------------------- writes

    def insert_version(
        self, record: VersionRecord, outcome: str, detail: str
    ) -> bool:
        """Atomically archive one version.

        The announcement row, version row, attachment blob, search-index rows
        and capture log entry commit in one transaction, so a version is
        never archived without being searchable. Returns False when that
        source revision was already archived (no new version row is created).
        Raises ArchiveStorageError on any storage failure, rolling back so no
        partial version can be observed.
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
            cursor = conn.execute(
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
            self._index_version_tx(
                conn,
                cursor.lastrowid,
                record.announcement_number,
                record.revision,
                record.title,
                record.body,
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
