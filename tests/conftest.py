"""Test fixtures: in-process demo site + archive wired over ASGI."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.capture import CaptureService  # noqa: E402
from app.source import HttpSourceClient  # noqa: E402
from app.store import ArchiveStore  # noqa: E402
from demo.main import create_app as create_demo_app  # noqa: E402
from demo.store import DemoStore  # noqa: E402


@pytest.fixture()
def demo_db(tmp_path: Path) -> Path:
    return tmp_path / "demo.db"


@pytest.fixture()
def archive_db(tmp_path: Path) -> Path:
    return tmp_path / "archive.db"


@pytest.fixture()
def demo(demo_db: Path):
    app = create_demo_app(str(demo_db))
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def demo_store(demo_db: Path) -> DemoStore:
    return DemoStore(demo_db)


@pytest.fixture()
def store(archive_db: Path) -> ArchiveStore:
    return ArchiveStore(archive_db)


@pytest.fixture()
def service(store: ArchiveStore, demo: TestClient) -> CaptureService:
    source = HttpSourceClient("http://demo.test", client=demo)
    return CaptureService(store, source)


def sample_attachment(text: str = "招标文件 v1") -> bytes:
    return ("示范附件\n" + text + "\n").encode("utf-8")
