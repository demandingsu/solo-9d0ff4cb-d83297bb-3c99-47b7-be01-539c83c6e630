"""Archive behaviour: revisions, retraction, conflicts, rejection, atomicity."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.capture import CaptureService
from app.domain import (
    ArchiveStorageError,
    RevisionMismatch,
    SourceInconsistent,
    SourceNotFound,
)
from app.store import ArchiveStore

NUMBER = "ZFCG-2026-0001"


def _edit(demo, *, title="市政道路照明设备采购招标公告", body="正文 v2",
          attachment=None, filename=None, content_type="text/plain; charset=utf-8"):
    data = {"title": title, "body": body}
    files = None
    if attachment is not None:
        files = {
            "attachment": (
                filename or "附件.txt",
                attachment,
                content_type,
            )
        }
    response = demo.post(
        f"/notice/{NUMBER}/edit",
        data=data,
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def test_first_capture_keeps_body_and_attachment(service, store, demo):
    result = service.capture(NUMBER)
    assert result.status == "created"
    record = store.get_version(NUMBER, result.revision)
    assert record is not None
    assert record.title.startswith("市政道路照明")
    assert b"ZFCG-2026-0001" in record.attachment_blob
    assert record.attachment_revision == record.body_revision == record.revision

    demo_attachment = demo.get(
        f"/api/notices/{NUMBER}/attachment"
    ).content
    assert record.attachment_blob == demo_attachment


def test_same_revision_not_duplicated(service, store):
    first = service.capture(NUMBER)
    second = service.capture(NUMBER)
    assert second.status == "unchanged"
    assert second.revision == first.revision
    assert len(store.list_versions(NUMBER)) == 1


def test_body_edit_creates_new_revision_keeps_old(service, store, demo):
    first = service.capture(NUMBER)
    first_record = store.get_version(NUMBER, first.revision)

    _edit(demo, body="更新后的正文：投标截止时间调整。")
    second = service.capture(NUMBER)

    assert second.status == "created"
    assert second.revision == first.revision + 1
    versions = store.list_versions(NUMBER)
    assert [v.revision for v in versions] == [second.revision, first.revision]
    # Old revision is byte-for-byte unchanged and still downloadable.
    old = store.get_version(NUMBER, first.revision)
    assert old.body == first_record.body
    assert old.attachment_blob == first_record.attachment_blob
    assert old.content_hash == first_record.content_hash


def test_attachment_replacement_archives_new_blob(service, store, demo):
    service.capture(NUMBER)
    new_blob = "替换后的附件内容".encode("utf-8")
    response = demo.post(
        f"/notice/{NUMBER}/attachment",
        files={"attachment": ("新版附件.txt", new_blob, "text/plain; charset=utf-8")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    result = service.capture(NUMBER)
    assert result.status == "created"
    record = store.get_version(NUMBER, result.revision)
    assert record.attachment_blob == new_blob
    assert record.attachment_revision == record.revision
    # Older attachment remains intact.
    old = store.get_version(NUMBER, result.revision - 1)
    assert old.attachment_blob != new_blob


def test_retraction_keeps_old_version_downloadable(service, store, demo):
    first = service.capture(NUMBER)
    response = demo.post(
        f"/notice/{NUMBER}/retract", data={"action": "retract"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    result = service.capture(NUMBER)
    # Same revision + same content: only the retraction state changes.
    assert result.status == "retracted_update"
    assert len(store.list_versions(NUMBER)) == 1
    announcement = store.get_announcement(NUMBER)
    assert announcement["source_retracted"] == 1

    old = store.get_version(NUMBER, first.revision)
    assert old is not None
    assert old.attachment_blob  # bytes still present after withdrawal


def test_retraction_before_first_capture_archives_with_flag(service, store, demo):
    demo.post(f"/notice/{NUMBER}/retract", data={"action": "retract"},
              follow_redirects=False)
    result = service.capture(NUMBER)
    assert result.status == "created"
    record = store.get_version(NUMBER, result.revision)
    assert record.source_retracted is True
    announcement = store.get_announcement(NUMBER)
    assert announcement["source_retracted"] == 1


def test_revision_mismatch_rejects_whole_capture(service, store, demo):
    first = service.capture(NUMBER)
    before = store.get_version(NUMBER, first.revision)

    demo.post(
        f"/demo/{NUMBER}/skew",
        data={"body_revision": 5, "attachment_revision": 4},
        follow_redirects=False,
    )

    with pytest.raises(RevisionMismatch):
        service.capture(NUMBER)

    versions = store.list_versions(NUMBER)
    assert [v.revision for v in versions] == [first.revision]
    after = store.get_version(NUMBER, first.revision)
    assert after.body == before.body
    assert after.attachment_blob == before.attachment_blob


def test_same_revision_different_content_reports_conflict(service, store, demo):
    first = service.capture(NUMBER)
    before = store.get_version(NUMBER, first.revision)

    demo.post(
        f"/demo/{NUMBER}/conflict",
        data={"body": "同号改写后的正文"},
        follow_redirects=False,
    )

    result = service.capture(NUMBER)
    assert result.status == "conflict"
    assert result.revision == first.revision

    versions = store.list_versions(NUMBER)
    assert len(versions) == 1
    after = store.get_version(NUMBER, first.revision)
    assert after.body == before.body
    assert after.attachment_blob == before.attachment_blob
    assert after.content_hash == before.content_hash


def test_source_revision_rollback_is_reported_not_archived(service, store, demo):
    service.capture(NUMBER)
    _edit(demo, body="第二版正文")
    second = service.capture(NUMBER)
    assert second.revision == 2

    # Rewind the demo site below the archived revision.
    demo_store = __import__("demo.store", fromlist=["DemoStore"]).DemoStore(
        Path(demo.app.state.store.db_path)
    )
    demo_store.reset_revision(NUMBER, 1)

    result = service.capture(NUMBER)
    assert result.status == "conflict"
    assert [v.revision for v in store.list_versions(NUMBER)] == [2, 1]


def test_attachment_hash_mismatch_rejects_capture(service, store, demo, monkeypatch):
    service.capture(NUMBER)
    original = __import__("demo.main", fromlist=["DemoStore"])

    # Patch the demo's stored SHA so metadata no longer matches bytes.
    db_path = demo.app.state.store.db_path
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE attachments SET sha256 = ? WHERE notice_number = ?",
        ("0" * 64, NUMBER),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SourceInconsistent):
        service.capture(NUMBER)
    # Metadata changed but no new version was created.
    assert len(store.list_versions(NUMBER)) == 1


def test_unknown_number_raises_not_found(service, store):
    with pytest.raises(SourceNotFound):
        service.capture("NO-SUCH-0009")
    assert store.list_versions("NO-SUCH-0009") == []
    assert store.get_announcement("NO-SUCH-0009") is None


def test_invalid_number_rejected_before_network(service):
    from app.domain import InvalidNumber

    with pytest.raises(InvalidNumber):
        service.capture("../etc/passwd")


def test_storage_failure_leaves_no_partial_version(service, store, demo, monkeypatch):
    service.capture(NUMBER)
    _edit(demo, body="第三版正文，将触发写入失败")

    real_connect = store._connect

    class FailingConnection:
        """Proxy that fails on the version INSERT, delegating everything else."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *params):
            if "INSERT INTO versions" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *params)

    def failing_connect():
        return FailingConnection(real_connect())

    monkeypatch.setattr(store, "_connect", failing_connect)
    with pytest.raises(ArchiveStorageError):
        service.capture(NUMBER)
    monkeypatch.setattr(store, "_connect", real_connect)

    versions = store.list_versions(NUMBER)
    assert [v.revision for v in versions] == [1]
    assert not any(v.body.startswith("第三版") for v in versions)


def test_interrupted_write_rolls_back(archive_db: Path):
    """Simulate a crash mid-transaction: SQLite must keep the old snapshot."""
    store = ArchiveStore(archive_db)
    from app.domain import VersionRecord

    demo_store_path = archive_db.with_name("demo-for-rollback.db")
    from demo.store import DemoStore

    demo_store = DemoStore(demo_store_path)
    notice = demo_store.create(
        number=NUMBER,
        title="回滚测试",
        body="正文一",
        attachment_filename="a.txt",
        attachment_content_type="text/plain",
        attachment_blob=b"blob-one",
    )
    from app.domain import AttachmentSnapshot, SourceSnapshot
    import hashlib

    def snapshot(rev: int, body: str) -> SourceSnapshot:
        blob = f"blob-{rev}".encode()
        return SourceSnapshot(
            number=NUMBER,
            title=notice.title,
            body=body,
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            revision=rev,
            body_revision=rev,
            attachment_revision=rev,
            retracted=False,
            source_url="http://demo.test/api/notices/x",
            attachment=AttachmentSnapshot(
                filename="a.txt",
                content_type="text/plain",
                size=len(blob),
                sha256=hashlib.sha256(blob).hexdigest(),
                data=blob,
            ),
        )

    store.insert_version(
        VersionRecord.from_snapshot(snapshot(1, "正文一"), "2026-01-01T00:00:00Z"),
        outcome="created",
        detail="seed",
    )

    # Open a transaction directly and leave it uncommitted, as a process kill
    # between INSERT and COMMIT would.
    conn = sqlite3.connect(archive_db, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    record = VersionRecord.from_snapshot(
        snapshot(2, "正文二"), "2026-01-02T00:00:00Z"
    )
    conn.execute(
        """
        INSERT INTO versions (
            announcement_number, revision, body_revision, attachment_revision,
            title, body, body_sha256, content_hash, attachment_filename,
            attachment_content_type, attachment_size, attachment_sha256,
            attachment_blob, source_retracted, source_url, source_updated_at,
            captured_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            record.announcement_number, record.revision, record.body_revision,
            record.attachment_revision, record.title, record.body,
            record.body_sha256, record.content_hash, record.attachment_filename,
            record.attachment_content_type, record.attachment_size,
            record.attachment_sha256, sqlite3.Binary(record.attachment_blob),
            0, record.source_url, record.source_updated_at, record.captured_at,
        ),
    )
    conn.close()  # uncommitted -> SQLite rolls back

    reopened = ArchiveStore(archive_db)
    assert [v.revision for v in reopened.list_versions(NUMBER)] == [1]
    assert reopened.get_version(NUMBER, 2) is None


def test_concurrent_duplicate_capture_creates_one_version(service, store, demo, tmp_path):
    """Two captures racing on the same revision must not double-insert."""
    from app.source import HttpSourceClient

    second_store = ArchiveStore(tmp_path / "archive2.db")
    client = HttpSourceClient("http://demo.test", client=demo)
    other = CaptureService(second_store, client)

    r1 = service.capture(NUMBER)
    r2 = other.capture(NUMBER)
    assert r1.status == "created"
    assert r2.status == "created"
    assert len(store.list_versions(NUMBER)) == 1
    assert len(second_store.list_versions(NUMBER)) == 1


def test_content_identical_but_retraction_toggles_back(service, store, demo):
    service.capture(NUMBER)
    demo.post(f"/notice/{NUMBER}/retract", data={"action": "retract"},
              follow_redirects=False)
    assert service.capture(NUMBER).status == "retracted_update"
    demo.post(f"/notice/{NUMBER}/retract", data={"action": "restore"},
              follow_redirects=False)
    result = service.capture(NUMBER)
    assert result.status == "retracted_update"
    assert store.get_announcement(NUMBER)["source_retracted"] == 0
    assert len(store.list_versions(NUMBER)) == 1


def test_concurrent_same_revision_different_content_reports_conflict(
    service, store, demo, tmp_path
):
    """If another writer already stored this revision with other content,
    the loser must report a conflict rather than silently say 'unchanged'."""
    from app.domain import (
        AttachmentSnapshot,
        SourceSnapshot,
        VersionRecord,
    )
    import hashlib

    service.capture(NUMBER)  # baseline r1

    # Simulate the winning concurrent writer: a different body under r2.
    blob = b"winner-blob"
    body = "并发写入者保存的正文"
    winner = SourceSnapshot(
        number=NUMBER,
        title="并发标题",
        body=body,
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        revision=2,
        body_revision=2,
        attachment_revision=2,
        retracted=False,
        source_url="u",
        attachment=AttachmentSnapshot(
            "w.txt", "text/plain", len(blob),
            hashlib.sha256(blob).hexdigest(), blob,
        ),
    )
    store.insert_version(
        VersionRecord.from_snapshot(winner, "2026-01-01T00:00:00Z"),
        outcome="created",
        detail="winner",
    )

    # Our capture now fetches r2 with different content (edit the demo once
    # more so its revision is above the archived baseline).
    _edit(demo, body="并发写入者保存的正文-mine")
    demo.post(
        f"/demo/{NUMBER}/skew",
        data={"body_revision": 2, "attachment_revision": 2},
        follow_redirects=False,
    )
    # Rebuild demo state: replace r2 content under same number.
    demo.app.state.store.update_content(
        NUMBER,
        title="并发标题",
        body="另一个并发者的正文",
        bump_revision=False,
    )

    result = service.capture(NUMBER)
    assert result.status == "conflict"
    stored = store.get_version(NUMBER, 2)
    assert stored is not None and stored.body == body
    assert len(store.list_versions(NUMBER)) == 2
