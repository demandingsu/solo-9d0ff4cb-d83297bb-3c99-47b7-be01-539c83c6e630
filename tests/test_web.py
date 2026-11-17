"""Archive web UI and download behaviour."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app

NUMBER = "ZFCG-2026-0001"


def _archive_app(tmp_path: Path, demo):
    db = tmp_path / "archive-web.db"
    app = create_app(
        db_path=str(db),
        source_base_url="http://demo.test",
        source_client=demo,
    )
    return app


def test_capture_via_form_then_view_and_download(tmp_path, demo):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        response = client.post("/capture", data={"number": NUMBER},
                               follow_redirects=False)
        assert response.status_code == 303
        assert "/notice/" in response.headers["location"]

        page = client.get(f"/notice/{NUMBER}")
        assert page.status_code == 200
        assert "市政道路照明设备采购招标公告" in page.text
        assert "版本历史" in page.text

        download = client.get(f"/notice/{NUMBER}/version/1/attachment")
        assert download.status_code == 200
        assert download.content == demo.get(
            f"/api/notices/{NUMBER}/attachment"
        ).content
        assert download.headers["x-content-sha256"]


def test_unknown_archive_number_shows_not_found(tmp_path, demo):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        page = client.get("/notice/NOPE-1")
        assert page.status_code == 404
        assert "未找到" in page.text


def test_capture_error_does_not_touch_archive(tmp_path, demo):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        response = client.post(
            "/capture", data={"number": "MISSING-404"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        page = client.get(response.headers["location"])
        assert "没有找到该公告号" in page.text
        assert app.state.store.list_announcements() == []


def test_conflict_is_shown_and_old_version_stays_downloadable(tmp_path, demo):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        client.post("/capture", data={"number": NUMBER})
        old = client.get(f"/notice/{NUMBER}/version/1/attachment").content

        demo.post(
            f"/demo/{NUMBER}/conflict",
            data={"body": "同号改写正文"},
            follow_redirects=False,
        )
        response = client.post(
            "/capture", data={"number": NUMBER}, follow_redirects=False
        )
        assert "status=conflict" in response.headers["location"]

        page = client.get(f"/notice/{NUMBER}?status=conflict")
        assert "源站冲突" in page.text
        current = client.get(f"/notice/{NUMBER}/version/1/attachment")
        assert current.content == old


def test_retracted_notice_page_keeps_history(tmp_path, demo):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        client.post("/capture", data={"number": NUMBER})
        demo.post(f"/notice/{NUMBER}/retract", data={"action": "retract"},
                  follow_redirects=False)
        client.post("/capture", data={"number": NUMBER})
        page = client.get(f"/notice/{NUMBER}")
        assert "源站已撤稿" in page.text
        assert client.get(f"/notice/{NUMBER}/version/1/attachment").status_code == 200


def test_healthz(tmp_path, demo):
    app = _archive_app(tmp_path, demo)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
