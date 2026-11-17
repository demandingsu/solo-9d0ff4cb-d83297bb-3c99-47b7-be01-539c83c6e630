"""Demo source site storage.

A notice has exactly one attachment. Every edit that changes content creates
a new site revision and bumps body_revision and attachment_revision together.
Demo-only knobs may deliberately desynchronise them to exercise the archive's
rejection path.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.domain import utc_now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    number               TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT NOT NULL,
    revision             INTEGER NOT NULL,
    body_revision        INTEGER NOT NULL,
    attachment_revision  INTEGER NOT NULL,
    retracted            INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    notice_number   TEXT PRIMARY KEY REFERENCES notices(number),
    filename        TEXT NOT NULL,
    content_type    TEXT NOT NULL,
    size            INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    blob            BLOB NOT NULL,
    revision        INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class DemoAttachment:
    filename: str
    content_type: str
    size: int
    sha256: str
    blob: bytes
    revision: int


@dataclass(frozen=True)
class DemoNotice:
    number: str
    title: str
    body: str
    revision: int
    body_revision: int
    attachment_revision: int
    retracted: bool
    updated_at: str
    attachment: DemoAttachment

    def public_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "revision": self.revision,
            "body_revision": self.body_revision,
            "attachment_revision": self.attachment_revision,
            "retracted": self.retracted,
            "updated_at": self.updated_at,
            "attachment": {
                "filename": self.attachment.filename,
                "content_type": self.attachment.content_type,
                "size": self.attachment.size,
                "sha256": self.attachment.sha256,
                "download_url": (
                    f"/api/notices/{self.number}/attachment"
                    f"?rev={self.attachment.revision}"
                ),
            },
        }


class DemoStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    # ---------------------------------------------------------------- reads

    def get(self, number: str) -> DemoNotice | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT n.*, a.filename AS a_filename,
                       a.content_type AS a_content_type, a.size AS a_size,
                       a.sha256 AS a_sha256, a.blob AS a_blob,
                       a.revision AS a_revision
                FROM notices n
                LEFT JOIN attachments a ON a.notice_number = n.number
                WHERE n.number = ?
                """,
                (number,),
            ).fetchone()
        finally:
            conn.close()
        return self._row(row) if row else None

    def list(self) -> list[DemoNotice]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT n.*, a.filename AS a_filename,
                       a.content_type AS a_content_type, a.size AS a_size,
                       a.sha256 AS a_sha256, a.blob AS a_blob,
                       a.revision AS a_revision
                FROM notices n
                LEFT JOIN attachments a ON a.notice_number = n.number
                ORDER BY n.number
                """
            ).fetchall()
        finally:
            conn.close()
        return [self._row(r) for r in rows]

    def attachment_bytes(
        self, number: str, revision: int | None = None
    ) -> tuple[str, str, bytes] | None:
        conn = self._connect()
        try:
            if revision is None:
                row = conn.execute(
                    "SELECT * FROM attachments WHERE notice_number = ?",
                    (number,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM attachments WHERE notice_number = ? "
                    "AND revision = ?",
                    (number, revision),
                ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return row["filename"], row["content_type"], bytes(row["blob"])

    def exists(self, number: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM notices WHERE number = ?", (number,)
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    # --------------------------------------------------------------- writes

    def create(
        self,
        *,
        number: str,
        title: str,
        body: str,
        attachment_filename: str,
        attachment_content_type: str,
        attachment_blob: bytes,
    ) -> DemoNotice:
        if self.exists(number):
            raise ValueError(f"公告号 {number} 已存在")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO notices
                    (number, title, body, revision, body_revision,
                     attachment_revision, retracted, updated_at)
                VALUES (?, ?, ?, 1, 1, 1, 0, ?)
                """,
                (number, title, body, now),
            )
            self._write_attachment(
                conn, number, attachment_filename, attachment_content_type,
                attachment_blob, revision=1,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        notice = self.get(number)
        assert notice is not None
        return notice

    def update_content(
        self,
        number: str,
        *,
        title: str,
        body: str,
        attachment_filename: str | None = None,
        attachment_content_type: str | None = None,
        attachment_blob: bytes | None = None,
        bump_revision: bool = True,
    ) -> DemoNotice:
        """Publish a new revision. Latest attachment kept when not replaced.

        ``bump_revision=False`` is a demo-only way to republish different
        content under the same revision number so the archive's conflict
        handling can be exercised.
        """
        current = self.get(number)
        if current is None:
            raise KeyError(number)
        new_rev = current.revision + 1 if bump_revision else current.revision
        replaces_attachment = attachment_blob is not None
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE notices SET title = ?, body = ?, revision = ?,
                    body_revision = ?, attachment_revision = ?, updated_at = ?
                WHERE number = ?
                """,
                (
                    title,
                    body,
                    new_rev,
                    new_rev,
                    new_rev,
                    utc_now_iso(),
                    number,
                ),
            )
            if replaces_attachment:
                self._write_attachment(
                    conn,
                    number,
                    attachment_filename or "attachment.bin",
                    attachment_content_type or "application/octet-stream",
                    attachment_blob,
                    revision=new_rev,
                )
            else:
                # Keep identical bytes but mark them as belonging to the new
                # revision, consistent with a body-only edit.
                conn.execute(
                    "UPDATE attachments SET revision = ? WHERE notice_number = ?",
                    (new_rev, number),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        notice = self.get(number)
        assert notice is not None
        return notice

    def replace_attachment(
        self,
        number: str,
        *,
        attachment_filename: str,
        attachment_content_type: str,
        attachment_blob: bytes,
    ) -> DemoNotice:
        current = self.get(number)
        if current is None:
            raise KeyError(number)
        new_rev = current.revision + 1
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE notices SET revision = ?, body_revision = ?,
                    attachment_revision = ?, updated_at = ?
                WHERE number = ?
                """,
                (new_rev, new_rev, new_rev, utc_now_iso(), number),
            )
            self._write_attachment(
                conn, number, attachment_filename, attachment_content_type,
                attachment_blob, revision=new_rev,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        notice = self.get(number)
        assert notice is not None
        return notice

    def set_retracted(self, number: str, retracted: bool) -> DemoNotice:
        """Withdraw/restore without changing revision or content.

        This mirrors real sites where a takedown notice replaces the page while
        the archived document itself is unchanged.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE notices SET retracted = ?, updated_at = ? WHERE number = ?",
                (int(retracted), utc_now_iso(), number),
            )
            if cursor.rowcount != 1:
                raise KeyError(number)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        notice = self.get(number)
        assert notice is not None
        return notice

    def force_revision_skew(
        self, number: str, *, body_revision: int, attachment_revision: int
    ) -> None:
        """Deliberate inconsistency for demonstrating capture rejection."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE notices SET body_revision = ?, attachment_revision = ? "
                "WHERE number = ?",
                (body_revision, attachment_revision, number),
            )
        finally:
            conn.close()

    def reset_revision(self, number: str, revision: int) -> None:
        """Rewind the published revision to demonstrate a source conflict."""
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE notices SET revision = ?, body_revision = ?,
                    attachment_revision = ?, updated_at = ?
                WHERE number = ?
                """,
                (revision, revision, revision, utc_now_iso(), number),
            )
        finally:
            conn.close()

    @staticmethod
    def _write_attachment(
        conn: sqlite3.Connection,
        number: str,
        filename: str,
        content_type: str,
        blob: bytes,
        *,
        revision: int,
    ) -> None:
        digest = hashlib.sha256(blob).hexdigest()
        conn.execute(
            """
            INSERT INTO attachments
                (notice_number, filename, content_type, size, sha256, blob,
                 revision)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(notice_number) DO UPDATE SET
                filename = excluded.filename,
                content_type = excluded.content_type,
                size = excluded.size,
                sha256 = excluded.sha256,
                blob = excluded.blob,
                revision = excluded.revision
            """,
            (
                number,
                filename,
                content_type,
                len(blob),
                digest,
                sqlite3.Binary(blob),
                revision,
            ),
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> DemoNotice:
        if row["a_blob"] is None:
            raise RuntimeError(f"公告 {row['number']} 缺少附件")
        return DemoNotice(
            number=row["number"],
            title=row["title"],
            body=row["body"],
            revision=row["revision"],
            body_revision=row["body_revision"],
            attachment_revision=row["attachment_revision"],
            retracted=bool(row["retracted"]),
            updated_at=row["updated_at"],
            attachment=DemoAttachment(
                filename=row["a_filename"],
                content_type=row["a_content_type"],
                size=row["a_size"],
                sha256=row["a_sha256"],
                blob=bytes(row["a_blob"]),
                revision=row["a_revision"],
            ),
        )
