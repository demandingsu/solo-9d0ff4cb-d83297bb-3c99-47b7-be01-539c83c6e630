"""Environment-driven application wiring."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .capture import CaptureService
from .domain import (
    MAX_QUERY_CHARS,
    MIN_QUERY_CHARS,
    ArchiveStorageError,
    InvalidNumber,
    SearchNotReady,
    SourceError,
    format_bytes,
    format_timestamp,
)
from .source import HttpSourceClient
from .store import ArchiveStore

BASE_DIR = Path(__file__).resolve().parent

SEARCH_RESULT_LIMIT = 200


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    return value or default


def create_app(
    *,
    db_path: str | None = None,
    source_base_url: str | None = None,
    source_client=None,
    auto_index: bool = True,
) -> FastAPI:
    store = ArchiveStore(
        db_path or _env("ARCHIVE_DB_PATH", "./data/archive.db"),
        auto_index=auto_index,
    )
    source = HttpSourceClient(
        source_base_url or _env("SOURCE_BASE_URL", "http://127.0.0.1:8000"),
        client=source_client,
    )
    service = CaptureService(store, source)
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.filters["bytes"] = format_bytes
    templates.env.filters["ts"] = format_timestamp

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        source.close()

    app = FastAPI(
        title="市政采购公告档案",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.source = source
    app.state.service = service

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "announcements": store.list_announcements(),
                "events": store.recent_events(),
                "search_status": store.search_status(),
                "captured": request.query_params.get("captured"),
                "error": request.query_params.get("error"),
            },
        )

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request):
        q = request.query_params.get("q")
        context = {
            "q": q or "",
            "search_status": store.search_status(),
            "results": None,
            "truncated": False,
            "error": None,
        }
        context["status_message"] = _search_status_message(
            context["search_status"]
        )
        if not q:
            return templates.TemplateResponse(request, "search.html", context)
        if len(q) < MIN_QUERY_CHARS:
            context["error"] = (
                f"请至少输入 {MIN_QUERY_CHARS} 个字符再检索"
                f"（当前 {len(q)} 个）。标点与空白也按字符计。"
            )
            return templates.TemplateResponse(request, "search.html", context)
        if len(q) > MAX_QUERY_CHARS:
            context["error"] = (
                f"检索内容过长：最多 {MAX_QUERY_CHARS} 个字符。"
            )
            return templates.TemplateResponse(request, "search.html", context)
        if not context["search_status"]["ready"]:
            return templates.TemplateResponse(request, "search.html", context)
        try:
            hits, truncated = store.search_versions(
                q, limit=SEARCH_RESULT_LIMIT
            )
        except SearchNotReady:
            # The index flipped to not-ready between the check and the query;
            # re-read the state and show the honest status page.
            context["search_status"] = store.search_status()
            context["status_message"] = _search_status_message(
                context["search_status"]
            )
            return templates.TemplateResponse(request, "search.html", context)
        except ArchiveStorageError as exc:
            context["error"] = exc.message
            return templates.TemplateResponse(request, "search.html", context)
        context["results"] = hits
        context["truncated"] = truncated
        return templates.TemplateResponse(request, "search.html", context)

    @app.post("/search/reindex")
    def reindex():
        try:
            store.backfill_search_index()
        except ArchiveStorageError:
            pass  # state row already records the failure; the page shows it
        return RedirectResponse("/search", status_code=303)

    @app.post("/capture")
    def capture(number: str = Form(...)):
        try:
            result = service.capture(number)
        except InvalidNumber as exc:
            return _error_redirect("", exc.message)
        except (SourceError, ArchiveStorageError) as exc:
            return _error_redirect(number.strip(), exc.message)
        return RedirectResponse(
            f"/notice/{quote(result.number, safe='')}?status={result.status}",
            status_code=303,
        )

    @app.get("/notice/{number}", response_class=HTMLResponse)
    def notice(request: Request, number: str):
        versions = store.list_versions(number)
        if not versions:
            return templates.TemplateResponse(
                request,
                "not_found.html",
                {"number": number, "error": request.query_params.get("error")},
                status_code=404,
            )
        status = request.query_params.get("status")
        return templates.TemplateResponse(
            request,
            "notice.html",
            {
                "number": number,
                "versions": versions,
                "latest": versions[0],
                "status": status,
                "status_message": _STATUS_MESSAGES.get(status),
                "events": store.events_for(number),
                "error": request.query_params.get("error"),
                "announcement": store.get_announcement(number),
            },
        )

    @app.get("/notice/{number}/version/{revision}", response_class=HTMLResponse)
    def version(request: Request, number: str, revision: int):
        record = store.get_version(number, revision)
        if record is None:
            return templates.TemplateResponse(
                request,
                "not_found.html",
                {"number": number, "error": "该版本不存在"},
                status_code=404,
            )
        all_versions = store.list_versions(number)
        return templates.TemplateResponse(
            request,
            "version.html",
            {"record": record, "versions": all_versions},
        )

    @app.get("/notice/{number}/version/{revision}/attachment")
    def download(number: str, revision: int):
        record = store.get_version(number, revision)
        if record is None:
            raise HTTPException(status_code=404, detail="该版本附件不存在")
        return Response(
            content=record.attachment_blob,
            media_type=record.attachment_content_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{_ascii_filename(record.attachment_filename)}"; '
                    f"filename*=UTF-8''{quote(record.attachment_filename, safe='')}"
                ),
                "X-Content-SHA256": record.attachment_sha256,
                "Cache-Control": "private, max-age=0, must-revalidate",
            },
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


_STATUS_MESSAGES = {
    "created": "采集成功：已在同一事务中保存正文与附件。",
    "unchanged": "相同修订号且内容一致：未重复建版。",
    "conflict": "采集被拒：源站冲突，旧版未覆盖。",
    "retracted_update": "已记录源站撤稿状态变化，未新建版本。",
}


def _search_status_message(status: dict) -> str | None:
    if status["ready"]:
        return None
    progress = ""
    if status.get("indexed") is not None and status.get("total") is not None:
        progress = f"（已建索引 {status['indexed']} / {status['total']} 个版本）"
    if status["status"] == "failed":
        return f"检索索引补建失败{progress}：{status.get('detail') or '未知原因'}"
    return f"检索索引补建中{progress}，暂未就绪"


def _error_redirect(number: str, message: str) -> RedirectResponse:
    target = "/?error=" + quote(message, safe="")
    if number:
        target = f"/notice/{quote(number, safe='')}?error=" + quote(message, safe="")
    return RedirectResponse(target, status_code=303)


def _ascii_filename(name: str) -> str:
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name)
    return fallback or "attachment.bin"

