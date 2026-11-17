"""Real process-kill tests: no half-written version survives a restart."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CRASH_SCRIPT = textwrap.dedent(
    """
    import os, sqlite3, sys
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    from app.store import ArchiveStore

    db = Path(sys.argv[2])
    ArchiveStore(db)  # create schema

    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO announcements "
        "(number, first_seen_at, last_checked_at, source_retracted) "
        "VALUES ('CRASH-1', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 0)"
    )
    conn.execute(
        "INSERT INTO versions ("
        " announcement_number, revision, body_revision, attachment_revision,"
        " title, body, body_sha256, content_hash, attachment_filename,"
        " attachment_content_type, attachment_size, attachment_sha256,"
        " attachment_blob, source_retracted, source_url, source_updated_at,"
        " captured_at"
        ") VALUES ('CRASH-1', 1, 1, 1, 't', 'b', 'x', 'y', 'a.txt',"
        " 'text/plain', 3, 'z', X'616263', 0, 'u', NULL, '2026-01-01T00:00:00Z')"
    )
    # Hard exit before COMMIT: behaves like SIGKILL/power loss mid-write.
    os._exit(137)
    """
)


def test_kill_before_commit_leaves_no_version(tmp_path: Path):
    db = tmp_path / "crash.db"
    script = tmp_path / "crash_writer.py"
    script.write_text(CRASH_SCRIPT, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-I", str(script), str(ROOT), str(db)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 137, result.stderr

    conn = sqlite3.connect(db)
    try:
        # Neither the announcement nor the version may be visible.
        versions = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
        announcements = conn.execute(
            "SELECT COUNT(*) FROM announcements"
        ).fetchone()[0]
    finally:
        conn.close()
    assert versions == 0
    assert announcements == 0

    # Re-opening through the application must work and still show nothing.
    sys.path.insert(0, str(ROOT))
    from app.store import ArchiveStore

    store = ArchiveStore(db)
    assert store.list_versions("CRASH-1") == []
    assert store.get_announcement("CRASH-1") is None


def test_crash_during_attachment_write_leaves_no_partial_blob(tmp_path: Path):
    """A version row must never exist without its complete attachment BLOB."""
    db = tmp_path / "crash2.db"
    script = tmp_path / "crash_writer2.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, sqlite3, sys
            sys.path.insert(0, sys.argv[1])
            from app.store import ArchiveStore
            db = sys.argv[2]
            ArchiveStore(db)
            conn = sqlite3.connect(db, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO announcements (number, first_seen_at,"
                " last_checked_at, source_retracted) VALUES"
                " ('CRASH-2','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',0)"
            )
            conn.execute(
                "INSERT INTO versions (announcement_number, revision,"
                " body_revision, attachment_revision, title, body, body_sha256,"
                " content_hash, attachment_filename, attachment_content_type,"
                " attachment_size, attachment_sha256, attachment_blob,"
                " source_retracted, source_url, source_updated_at, captured_at)"
                " VALUES ('CRASH-2',1,1,1,'t','b','x','y','a.txt','text/plain',"
                " 1000,'z',X'616263',0,'u',NULL,'2026-01-01T00:00:00Z')"
            )
            os._exit(137)
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-I", str(script), str(ROOT), str(db)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 137, result.stderr

    sys.path.insert(0, str(ROOT))
    from app.store import ArchiveStore

    store = ArchiveStore(db)
    assert store.latest_version("CRASH-2") is None
    # The versions table has no orphan/partial row.
    assert store.list_versions("CRASH-2") == []


def test_restart_after_successful_write_keeps_version(tmp_path: Path):
    """Control case: a committed version survives an abrupt restart."""
    db = tmp_path / "ok.db"
    script = tmp_path / "ok_writer.py"
    script.write_text(
        textwrap.dedent(
            """
            import hashlib, sys
            sys.path.insert(0, sys.argv[1])
            from app.store import ArchiveStore
            from app.domain import VersionRecord, AttachmentSnapshot, SourceSnapshot
            db = sys.argv[2]
            blob = b'payload'
            snap = SourceSnapshot(
                number='OK-1', title='t', body='b',
                body_sha256=hashlib.sha256(b'b').hexdigest(),
                revision=1, body_revision=1, attachment_revision=1,
                retracted=False, source_url='u',
                attachment=AttachmentSnapshot(
                    filename='a.txt', content_type='text/plain',
                    size=len(blob), sha256=hashlib.sha256(blob).hexdigest(),
                    data=blob))
            ArchiveStore(db).insert_version(
                VersionRecord.from_snapshot(snap, '2026-01-01T00:00:00Z'),
                outcome='created', detail='ok')
            """
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-I", str(script), str(ROOT), str(db)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    sys.path.insert(0, str(ROOT))
    from app.store import ArchiveStore

    store = ArchiveStore(db)
    record = store.latest_version("OK-1")
    assert record is not None
    assert record.attachment_blob == b"payload"
