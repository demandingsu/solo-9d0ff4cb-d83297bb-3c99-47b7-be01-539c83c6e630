"""Local demo site: a stand-in procurement portal for the archive.

Pages (browser):
  /                     notice list + create form
  /notice/{number}      notice detail, edit body, replace attachment, retract

JSON API consumed by the archive:
  GET /api/notices/{number}
  GET /api/notices/{number}/attachment?rev=N
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.domain import (
    InvalidNumber,
    normalize_number,
    sanitize_content_type,
    sanitize_filename,
)
from demo.seed import ensure_seed
from demo.store import DemoStore

BASE_DIR = Path(__file__).resolve().parent


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("DEMO_DB_PATH", "./data/demo.db")
    store = DemoStore(db_path)
    ensure_seed(store)
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    app = FastAPI(title="采购公告示范站", docs_url="/api/docs")
    app.state.store = store

    @app.exception_handler(InvalidNumber)
    async def invalid_number_handler(_request: Request, exc: InvalidNumber):
        return JSONResponse(status_code=400, content={"detail": exc.message})

    # ------------------------------------------------------------- browser

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request,
            "index.html",
            {"notices": store.list()},
        )

    @app.get("/notice/{number}", response_class=HTMLResponse)
    def notice_page(request: Request, number: str):
        notice = store.get(number)
        if notice is None:
            return templates.TemplateResponse(
                request,
                "not_found.html",
                {"number": number},
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "notice.html",
            {"notice": notice},
        )

    @app.post("/notice/{number}/edit")
    def edit_notice(
        number: str,
        title: str = Form(...),
        body: str = Form(...),
        attachment: UploadFile | None = File(None),
    ):
        if store.get(number) is None:
            raise HTTPException(status_code=404, detail="公告不存在")
        title = title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="标题不能为空")
        if attachment is not None and attachment.filename:
            blob = attachment.file.read()
            if not blob:
                raise HTTPException(status_code=400, detail="附件不能为空")
            store.update_content(
                number,
                title=title,
                body=body,
                attachment_filename=sanitize_filename(attachment.filename),
                attachment_content_type=sanitize_content_type(
                    attachment.content_type
                ),
                attachment_blob=blob,
            )
        else:
            store.update_content(number, title=title, body=body)
        return RedirectResponse(
            f"/notice/{quote(number)}?saved=new-revision", status_code=303
        )

    @app.post("/notice/{number}/attachment")
    def replace_attachment(
        number: str,
        attachment: UploadFile = File(...),
    ):
        if store.get(number) is None:
            raise HTTPException(status_code=404, detail="公告不存在")
        blob = attachment.file.read()
        if not blob:
            raise HTTPException(status_code=400, detail="附件不能为空")
        store.replace_attachment(
            number,
            attachment_filename=sanitize_filename(
                attachment.filename or "attachment.bin"
            ),
            attachment_content_type=sanitize_content_type(
                attachment.content_type
            ),
            attachment_blob=blob,
        )
        return RedirectResponse(
            f"/notice/{quote(number)}?saved=attachment", status_code=303
        )

    @app.post("/notice/{number}/retract")
    def retract(number: str, action: str = Form(...)):
        if store.get(number) is None:
            raise HTTPException(status_code=404, detail="公告不存在")
        store.set_retracted(number, action == "retract")
        return RedirectResponse(
            f"/notice/{quote(number)}?saved="
            + ("retracted" if action == "retract" else "restored"),
            status_code=303,
        )

    @app.post("/create")
    def create_notice(
        number: str = Form(...),
        title: str = Form(...),
        body: str = Form(...),
        attachment: UploadFile = File(...),
    ):
        try:
            clean_number = normalize_number(number)
        except InvalidNumber as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        if store.exists(clean_number):
            raise HTTPException(
                status_code=409, detail=f"公告号 {clean_number} 已存在"
            )
        title = title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="标题不能为空")
        blob = attachment.file.read()
        if not blob:
            raise HTTPException(status_code=400, detail="附件不能为空")
        store.create(
            number=clean_number,
            title=title,
            body=body,
            attachment_filename=sanitize_filename(
                attachment.filename or "attachment.bin"
            ),
            attachment_content_type=sanitize_content_type(
                attachment.content_type
            ),
            attachment_blob=blob,
        )
        return RedirectResponse(
            f"/notice/{quote(clean_number)}?saved=created", status_code=303
        )

    # ----------------------------------------------------- demo-only knobs

    @app.post("/demo/{number}/skew")
    def force_skew(
        number: str,
        body_revision: int = Form(...),
        attachment_revision: int = Form(...),
    ):
        if store.get(number) is None:
            raise HTTPException(status_code=404, detail="公告不存在")
        store.force_revision_skew(
            number,
            body_revision=body_revision,
            attachment_revision=attachment_revision,
        )
        return RedirectResponse(
            f"/notice/{quote(number)}?saved=skew", status_code=303
        )

    @app.post("/demo/{number}/conflict")
    def force_conflict(number: str, body: str = Form(...)):
        """Republish different text without bumping the revision number."""
        current = store.get(number)
        if current is None:
            raise HTTPException(status_code=404, detail="公告不存在")
        store.update_content(
            number,
            title=current.title,
            body=body,
            bump_revision=False,
        )
        return RedirectResponse(
            f"/notice/{quote(number)}?saved=conflict", status_code=303
        )

    # ------------------------------------------------------------ JSON API

    @app.get("/api/notices/{number}")
    def api_notice(number: str):
        notice = store.get(number)
        if notice is None:
            raise HTTPException(status_code=404, detail="公告不存在")
        # A retracted notice stays readable but is flagged. The archive records
        # the flag while keeping every earlier version downloadable.
        return JSONResponse(notice.public_dict())

    @app.get("/api/notices/{number}/attachment")
    def api_attachment(number: str, rev: int | None = None):
        notice = store.get(number)
        if notice is None:
            raise HTTPException(status_code=404, detail="公告不存在")
        found = store.attachment_bytes(number, rev)
        if found is None:
            raise HTTPException(status_code=404, detail="附件不存在")
        filename, content_type, blob = found
        return Response(
            content=blob,
            media_type=content_type,
            headers={
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(filename)}"
                )
            },
        )

    return app

