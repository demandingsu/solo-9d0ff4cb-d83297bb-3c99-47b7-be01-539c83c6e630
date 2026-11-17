"""原文检索：字面匹配、索引一致性、旧库补建、中断/失败与交错行为。"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.capture import CaptureService
from app.domain import ArchiveStorageError
from app.main import create_app
from app.source import HttpSourceClient
from app.store import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
NUMBER = "ZFCG-2026-0001"


def _edit(demo, *, title="市政道路照明设备采购招标公告", body="正文 v2"):
    response = demo.post(
        f"/notice/{NUMBER}/edit",
        data={"title": title, "body": body},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _app(db: Path, demo, **kwargs):
    return create_app(
        db_path=str(db),
        source_base_url="http://demo.test",
        source_client=demo,
        **kwargs,
    )


def _wipe_index(db_path: Path) -> None:
    """Simulate a pre-search archive: versions exist, index is empty."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM search_docs")
        conn.execute("DELETE FROM search_grams")
        conn.execute("DELETE FROM search_state")
        conn.commit()
    finally:
        conn.close()


def _index_counts(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        return {
            "docs": conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0],
            "versions": conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0],
            "orphan_docs": conn.execute(
                "SELECT COUNT(*) FROM search_docs d WHERE NOT EXISTS "
                "(SELECT 1 FROM versions v WHERE v.id = d.version_id)"
            ).fetchone()[0],
            "missing_docs": conn.execute(
                "SELECT COUNT(*) FROM versions v WHERE NOT EXISTS "
                "(SELECT 1 FROM search_docs d WHERE d.version_id = v.id)"
            ).fetchone()[0],
        }
    finally:
        conn.close()


# ------------------------------------------------------------ 匹配语义


def test_search_lists_each_matching_revision_once(service, store, demo):
    service.capture(NUMBER)
    # "照明设备" appears in both the title and the body of r1.
    hits, truncated = store.search_versions("照明设备")
    assert not truncated
    matching = [h for h in hits if h.announcement_number == NUMBER]
    assert len(matching) == 1  # same revision listed only once
    assert matching[0].revision == 1
    assert matching[0].field == "title"  # title match preferred for snippet
    assert matching[0].hit == "照明设备"


def test_search_is_case_sensitive(service, store, demo):
    service.capture(NUMBER)
    _edit(demo, body="补充条款：ABCdef 投标人须知。")
    service.capture(NUMBER)  # r2

    hits, _ = store.search_versions("ABCdef")
    assert [(h.announcement_number, h.revision) for h in hits] == [(NUMBER, 2)]
    assert store.search_versions("abcdef")[0] == []
    assert store.search_versions("ABCDEF")[0] == []


def test_search_treats_punctuation_as_plain_characters(service, store, demo):
    service.capture(NUMBER)
    hits, _ = store.search_versions("09:30（北京时间）")
    assert [h.revision for h in hits] == [1]
    # Half-width parenthesis must not match the full-width one in the text.
    assert store.search_versions("09:30(北京时间）")[0] == []
    # Full-width colon is part of the text and must be given verbatim.
    hits, _ = store.search_versions("截止：2026")
    assert [h.revision for h in hits] == [1]
    assert store.search_versions("截止: 2026")[0] == []


def test_every_literal_substring_finds_its_version(service, store, demo):
    """The trigram narrowing must never drop a true match."""
    service.capture(NUMBER)
    record = store.get_version(NUMBER, 1)
    body = record.body
    for i in range(0, len(body) - 2, 7):
        needle = body[i : i + 3]
        hits, _ = store.search_versions(needle)
        assert (NUMBER, 1) in {
            (h.announcement_number, h.revision) for h in hits
        }, needle


def test_retracted_and_old_versions_remain_searchable(service, store, demo):
    service.capture(NUMBER)  # r1 contains "北京时间"
    demo.post(
        f"/notice/{NUMBER}/retract",
        data={"action": "retract"},
        follow_redirects=False,
    )
    assert service.capture(NUMBER).status == "retracted_update"
    hits, _ = store.search_versions("北京时间")
    assert [h.revision for h in hits] == [1]  # withdrawn version still found

    _edit(demo, body="全新正文，不再包含旧表述。")
    assert service.capture(NUMBER).status == "created"  # r2
    hits, _ = store.search_versions("北京时间")
    # The old revision stays searchable after being superseded.
    assert [(h.announcement_number, h.revision) for h in hits] == [(NUMBER, 1)]


# ------------------------------------------------------------ 网页行为


def test_search_page_validates_length_and_shows_form(tmp_path, demo):
    app = _app(tmp_path / "a.db", demo)
    with TestClient(app) as client:
        client.post("/capture", data={"number": NUMBER})

        page = client.get("/search")
        assert page.status_code == 200
        assert "原文检索" in page.text

        short = client.get("/search", params={"q": "照明"})
        assert short.status_code == 200
        assert "至少输入 3 个字符" in short.text
        assert "查看原版" not in short.text

        long = client.get("/search", params={"q": "长" * 1001})
        assert "检索内容过长" in long.text


def test_search_results_link_to_the_same_committed_revision(tmp_path, demo):
    app = _app(tmp_path / "a.db", demo)
    with TestClient(app) as client:
        client.post("/capture", data={"number": NUMBER})  # r1: 北京时间
        _edit(demo, body="第二版正文，不再出现旧时间表述。")
        client.post("/capture", data={"number": NUMBER})  # r2

        page = client.get("/search", params={"q": "北京时间"})
        assert page.status_code == 200
        assert "<mark>北京时间</mark>" in page.text
        assert f"/notice/{NUMBER}/version/1" in page.text
        assert f"/notice/{NUMBER}/version/2" not in page.text
        assert "r1" in page.text and NUMBER in page.text

        # The original-version entry resolves to the very revision listed.
        original = client.get(f"/notice/{NUMBER}/version/1")
        assert original.status_code == 200
        assert "北京时间" in original.text


def test_index_page_offers_search_form(tmp_path, demo):
    app = _app(tmp_path / "a.db", demo)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'action="/search"' in page.text


# ------------------------------------------------------------ 旧库补建


def test_old_database_is_backfilled_on_open(tmp_path, demo):
    db = tmp_path / "old.db"
    store = ArchiveStore(db)
    service = CaptureService(store, HttpSourceClient("http://demo.test", client=demo))
    service.capture(NUMBER)
    _edit(demo, body="第二版正文：新增 ABCdef 条款。")
    service.capture(NUMBER)

    # Simulate a database written before the search feature existed.
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE search_docs")
    conn.execute("DROP TABLE search_grams")
    conn.execute("DROP TABLE search_state")
    conn.commit()
    conn.close()

    reopened = ArchiveStore(db)  # first enablement: schema + backfill
    assert reopened.search_status()["ready"] is True
    hits, _ = reopened.search_versions("ABCdef")
    assert [(h.announcement_number, h.revision) for h in hits] == [(NUMBER, 2)]
    hits, _ = reopened.search_versions("北京时间")
    assert [h.revision for h in hits] == [1]
    counts = _index_counts(db)
    assert counts["docs"] == counts["versions"] == 2
    assert counts["missing_docs"] == counts["orphan_docs"] == 0


PARTIAL_BACKFILL_SCRIPT = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, sys.argv[1])
    from app.store import ArchiveStore
    store = ArchiveStore(sys.argv[2], auto_index=False)
    # Commit one chunk, then die like SIGKILL would mid-backfill.
    store.backfill_search_index(chunk_size=1, max_chunks=1)
    os._exit(137)
    """
)


def test_interrupted_backfill_reports_not_ready_then_recovers(tmp_path, demo):
    db = tmp_path / "interrupted.db"
    store = ArchiveStore(db, auto_index=False)
    service = CaptureService(store, HttpSourceClient("http://demo.test", client=demo))
    service.capture(NUMBER)
    _edit(demo, body="第二版正文：包含 待补建 关键词。")
    service.capture(NUMBER)
    _wipe_index(db)  # historical revisions without an index

    script = tmp_path / "partial_backfill.py"
    script.write_text(PARTIAL_BACKFILL_SCRIPT, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", str(script), str(ROOT), str(db)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 137, result.stderr

    # The archive opens, but search honestly reports it is not ready.
    app = _app(db, demo, auto_index=False)
    with TestClient(app) as client:
        status = app.state.store.search_status()
        assert status["ready"] is False
        assert status["status"] == "building"
        assert status["indexed"] < status["total"]

        page = client.get("/search", params={"q": "北京时间"})
        assert page.status_code == 200
        assert "未就绪" in page.text
        assert "查看原版" not in page.text  # no partial results shown

        # Capture and browsing still work while the index is incomplete.
        _edit(demo, body="第三版正文：中断期间采集。")
        resp = client.post("/capture", data={"number": NUMBER},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert "status=created" in resp.headers["location"]
        assert client.get(f"/notice/{NUMBER}/version/1").status_code == 200

        # Recover without restarting the app.
        resp = client.post("/search/reindex", follow_redirects=False)
        assert resp.status_code == 303
        assert app.state.store.search_status()["ready"] is True
        for needle in ("北京时间", "待补建", "中断期间采集"):
            page = client.get("/search", params={"q": needle})
            assert "查看原版" in page.text, needle
        counts = _index_counts(db)
        assert counts["docs"] == counts["versions"] == 3
        assert counts["missing_docs"] == 0


def test_failed_backfill_marks_not_ready_and_capture_still_works(
    tmp_path, demo, monkeypatch
):
    db = tmp_path / "failed.db"
    store = ArchiveStore(db, auto_index=False)
    service = CaptureService(store, HttpSourceClient("http://demo.test", client=demo))
    service.capture(NUMBER)
    _wipe_index(db)

    original = ArchiveStore._index_version_tx

    def boom(self, conn, *args):
        raise sqlite3.OperationalError("disk I/O error")

    # Startup backfill fails; the app must still come up and keep serving.
    monkeypatch.setattr(ArchiveStore, "_index_version_tx", boom)
    app = _app(db, demo)
    status = app.state.store.search_status()
    assert status["ready"] is False
    assert status["status"] == "failed"
    monkeypatch.setattr(ArchiveStore, "_index_version_tx", original)

    with TestClient(app) as client:
        page = client.get("/search", params={"q": "北京时间"})
        assert "未就绪" in page.text
        assert "disk I/O error" in page.text  # failure reason is shown
        assert "查看原版" not in page.text

        # Capture and original browsing are unaffected by the index failure.
        _edit(demo, body="失败期间的新正文。")
        resp = client.post("/capture", data={"number": NUMBER},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert "status=created" in resp.headers["location"]
        assert client.get(f"/notice/{NUMBER}").status_code == 200

        # Retrying the backfill after the "disk" recovers restores search.
        resp = client.post("/search/reindex", follow_redirects=False)
        assert resp.status_code == 303
        assert app.state.store.search_status()["ready"] is True
        page = client.get("/search", params={"q": "失败期间的新正文"})
        assert "查看原版" in page.text


# ------------------------------------------------------------ 一致性与交错


def test_index_write_failure_rolls_back_whole_version(
    service, store, demo, monkeypatch
):
    service.capture(NUMBER)  # r1 archived and indexed
    _edit(demo, body="第二版正文：触发索引写入失败。")

    def boom(self, conn, *args):
        raise sqlite3.OperationalError("disk I/O error")

    original = ArchiveStore._index_version_tx
    monkeypatch.setattr(ArchiveStore, "_index_version_tx", boom)
    with pytest.raises(ArchiveStorageError):
        service.capture(NUMBER)
    monkeypatch.setattr(ArchiveStore, "_index_version_tx", original)

    # The failed capture left nothing behind: no archived-but-unsearchable
    # version, no announcement row for a version that never committed.
    assert [v.revision for v in store.list_versions(NUMBER)] == [1]
    assert store.search_versions("触发索引写入失败")[0] == []
    counts = _index_counts(store.db_path)
    assert counts["docs"] == counts["versions"] == 1
    assert counts["missing_docs"] == counts["orphan_docs"] == 0

    # A later healthy capture archives r2 and makes it searchable at once.
    assert service.capture(NUMBER).status == "created"
    hits, _ = store.search_versions("触发索引写入失败")
    assert [h.revision for h in hits] == [2]


def test_interleaved_capture_and_search_point_to_committed_revisions(
    tmp_path, demo
):
    """Two writers on one database: search only sees committed revisions,
    and every hit resolves to a version row committed with it."""
    db = tmp_path / "interleaved.db"
    store_a = ArchiveStore(db)
    store_b = ArchiveStore(db)
    svc_a = CaptureService(store_a, HttpSourceClient("http://demo.test", client=demo))
    svc_b = CaptureService(store_b, HttpSourceClient("http://demo.test", client=demo))

    svc_a.capture(NUMBER)  # r1 committed via A
    hits, _ = store_b.search_versions("照明设备")
    assert [(h.announcement_number, h.revision) for h in hits] == [(NUMBER, 1)]

    _edit(demo, body="第二版正文：交错检索一致性。")
    svc_b.capture(NUMBER)  # r2 committed via B

    # Title still matches in both revisions; newest revision first.
    hits, _ = store_a.search_versions("照明设备")
    assert [(h.announcement_number, h.revision) for h in hits] == [
        (NUMBER, 2),
        (NUMBER, 1),
    ]
    for hit in hits:
        committed = store_a.get_version(hit.announcement_number, hit.revision)
        assert committed is not None  # hit and original entry stay in sync

    counts = _index_counts(db)
    assert counts["docs"] == counts["versions"] == 2
    assert counts["missing_docs"] == counts["orphan_docs"] == 0


def test_concurrent_capture_and_search_stay_consistent(tmp_path, demo):
    """Capture in one thread, search in another: every hit must reference a
    committed revision whose indexed text really contains the query."""
    import threading

    db = tmp_path / "concurrent.db"
    store = ArchiveStore(db)
    service = CaptureService(store, HttpSourceClient("http://demo.test", client=demo))
    service.capture(NUMBER)  # r1: contains 照明设备 and 北京时间

    failures: list[str] = []
    rounds = 8

    def search_loop():
        for _ in range(rounds * 4):
            try:
                hits, _ = store.search_versions("照明设备")
            except Exception as exc:  # noqa: BLE001 - record, assert later
                failures.append(f"search raised: {exc!r}")
                return
            for hit in hits:
                record = store.get_version(hit.announcement_number, hit.revision)
                if record is None:
                    failures.append(
                        f"hit r{hit.revision} has no committed version row"
                    )
                    return
                if (
                    "照明设备" not in record.title
                    and "照明设备" not in record.body
                ):
                    failures.append(
                        f"hit r{hit.revision} does not actually contain the query"
                    )
                    return

    worker = threading.Thread(target=search_loop)
    worker.start()
    for i in range(rounds):
        _edit(demo, body=f"第 {i} 批：照明设备条款调整，新增编号 {i}。")
        service.capture(NUMBER)
    worker.join()

    assert failures == []
    # Everything captured is searchable once the dust settles.
    hits, _ = store.search_versions("照明设备")
    assert len({h.revision for h in hits}) == rounds + 1
    counts = _index_counts(db)
    assert counts["docs"] == counts["versions"] == rounds + 1
    assert counts["missing_docs"] == counts["orphan_docs"] == 0
