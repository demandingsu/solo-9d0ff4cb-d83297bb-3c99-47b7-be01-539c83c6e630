"""HTTP source client for procurement announcement sites.

The client reads the notice metadata, downloads its single attachment, then
re-reads the metadata. A capture is only accepted when both reads agree and
the attachment bytes match the declared size and SHA-256. That prevents an
archive from mixing a body and attachment from different source revisions.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .domain import (
    AttachmentSnapshot,
    RevisionMismatch,
    SourceInconsistent,
    SourceNotFound,
    SourceSnapshot,
    SourceUnavailable,
    SourceWithdrawn,
    sanitize_content_type,
    sanitize_filename,
)

SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_TITLE_CHARS = 500
MAX_FILENAME_CHARS = 200
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


def _invalid(message: str) -> SourceInconsistent:
    return SourceInconsistent(f"源站返回的数据不完整或不合法：{message}；整次采集已拒收。")


def _positive_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _invalid(f"字段 {key} 必须是正整数")
    return value


def _text(data: dict[str, Any], key: str, *, max_chars: int) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise _invalid(f"字段 {key} 必须是字符串")
    if len(value) > max_chars:
        raise _invalid(f"字段 {key} 超过长度上限")
    return value


def _attachment_download_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid("附件缺少下载地址")
    if len(value) > 2048:
        raise _invalid("附件下载地址过长")
    return value.strip()


class HttpSourceClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        max_attachment_bytes: int = MAX_DOWNLOAD_BYTES,
    ):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("SOURCE_BASE_URL 必须是有效的 http(s) 地址")
        self.base_url = base_url.rstrip("/")
        self.max_attachment_bytes = max_attachment_bytes
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "municipal-procurement-archive/1.0"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch(self, number: str) -> SourceSnapshot:
        first = self._fetch_metadata(number)
        first_signature = self._signature(first)

        attachment = self._download_attachment(number, first)

        # Re-read after the download: the site must not have moved to a
        # different revision while we were capturing one.
        second = self._fetch_metadata(number)
        if self._signature(second) != first_signature:
            raise SourceInconsistent(
                "源站在采集过程中发生了修订变化，本次采集已拒收；"
                "档案中的旧版未受影响，请稍后重试。"
            )

        meta = first
        return SourceSnapshot(
            number=number,
            title=meta["title"],
            body=meta["body"],
            body_sha256=hashlib.sha256(
                meta["body"].encode("utf-8")
            ).hexdigest(),
            revision=meta["revision"],
            body_revision=meta["body_revision"],
            attachment_revision=meta["attachment_revision"],
            retracted=meta["retracted"],
            source_url=f"{self.base_url}/api/notices/{quote(number, safe='')}",
            attachment=attachment,
            source_updated_at=meta.get("updated_at"),
        )

    # ----------------------------------------------------------------- HTTP

    def _metadata_url(self, number: str) -> str:
        return f"{self.base_url}/api/notices/{quote(number, safe='')}"

    def _fetch_metadata(self, number: str) -> dict[str, Any]:
        url = self._metadata_url(number)
        try:
            response = self._client.get(url, headers={"Accept": "application/json"})
        except httpx.RequestError as exc:
            raise SourceUnavailable(
                f"无法连接源站：{type(exc).__name__}；本次采集未改动档案。"
            ) from exc

        if response.status_code == 404:
            raise SourceNotFound("源站没有找到该公告号。")
        if response.status_code == 410:
            raise SourceWithdrawn("源站显示该公告已撤稿或下架。")
        if response.status_code != 200:
            raise SourceUnavailable(
                f"源站返回 HTTP {response.status_code}；本次采集未改动档案。"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise _invalid("响应不是有效 JSON") from exc
        return self._parse_metadata(data, number)

    def _parse_metadata(
        self, data: Any, requested_number: str
    ) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise _invalid("响应顶层必须是对象")

        number = _text(data, "number", max_chars=100)
        if number != requested_number:
            raise _invalid("响应中的公告号与请求不一致")

        title = _text(data, "title", max_chars=MAX_TITLE_CHARS).strip()
        if not title:
            raise _invalid("公告标题为空")

        body = _text(data, "body", max_chars=MAX_BODY_BYTES)
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise _invalid("公告正文超过大小上限")

        revision = _positive_int(data, "revision")
        body_revision = _positive_int(data, "body_revision")
        attachment_revision = _positive_int(data, "attachment_revision")

        if len({revision, body_revision, attachment_revision}) != 1:
            raise RevisionMismatch(
                "源站正文与附件修订号不一致"
                f"（站点 {revision}，正文 {body_revision}，附件 {attachment_revision}），"
                "本次采集整次拒收；档案中的旧版未受影响。"
            )

        retracted = data.get("retracted", False)
        if not isinstance(retracted, bool):
            raise _invalid("字段 retracted 必须是布尔值")

        updated_at = data.get("updated_at")
        if updated_at is not None and (
            not isinstance(updated_at, str) or len(updated_at) > 100
        ):
            raise _invalid("字段 updated_at 不合法")

        attachment = data.get("attachment")
        if not isinstance(attachment, dict):
            raise _invalid("缺少附件信息")

        filename = sanitize_filename(
            _text(attachment, "filename", max_chars=MAX_FILENAME_CHARS)
        )
        if not filename:
            raise _invalid("附件文件名为空")
        content_type = sanitize_content_type(attachment.get("content_type"))

        size = attachment.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise _invalid("附件大小不合法")
        if size > self.max_attachment_bytes:
            raise _invalid("附件超过采集大小上限")

        sha256 = attachment.get("sha256")
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise _invalid("附件 SHA-256 不合法")

        return {
            "number": number,
            "title": title,
            "body": body,
            "revision": revision,
            "body_revision": body_revision,
            "attachment_revision": attachment_revision,
            "retracted": retracted,
            "updated_at": updated_at,
            "attachment": {
                "filename": filename,
                "content_type": content_type,
                "size": size,
                "sha256": sha256.lower(),
                "download_url": _attachment_download_url(
                    attachment.get("download_url")
                ),
            },
        }

    @staticmethod
    def _signature(meta: dict[str, Any]) -> tuple:
        attachment = meta["attachment"]
        return (
            meta["number"],
            meta["title"],
            hashlib.sha256(meta["body"].encode("utf-8")).hexdigest(),
            meta["revision"],
            meta["body_revision"],
            meta["attachment_revision"],
            meta["retracted"],
            meta["updated_at"],
            attachment["filename"],
            attachment["content_type"],
            attachment["size"],
            attachment["sha256"],
            attachment["download_url"],
        )

    def _download_attachment(
        self, number: str, meta: dict[str, Any]
    ) -> AttachmentSnapshot:
        info = meta["attachment"]
        raw_url = info["download_url"]
        url = urljoin(self._metadata_url(number), raw_url)
        base = urlsplit(self.base_url)
        target = urlsplit(url)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            raise _invalid("附件下载地址不属于源站")

        declared_size = info["size"]
        if declared_size > self.max_attachment_bytes:
            raise _invalid("附件超过采集大小上限")

        chunks: list[bytes] = []
        total = 0
        try:
            with self._client.stream("GET", url) as response:
                if response.status_code == 404:
                    raise SourceInconsistent(
                        "源站元数据指向的附件不存在，本次采集已拒收；"
                        "档案中的旧版未受影响。"
                    )
                if response.status_code != 200:
                    raise SourceUnavailable(
                        f"附件下载返回 HTTP {response.status_code}；"
                        "本次采集未改动档案。"
                    )
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.max_attachment_bytes:
                        raise _invalid("附件实际大小超过采集上限")
                    chunks.append(chunk)
        except httpx.RequestError as exc:
            raise SourceUnavailable(
                f"附件下载失败：{type(exc).__name__}；本次采集未改动档案。"
            ) from exc

        data = b"".join(chunks)
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != declared_size or digest != info["sha256"]:
            raise SourceInconsistent(
                "附件实际内容与源站声明的长度或 SHA-256 不一致，整次采集已拒收；"
                "档案中的旧版未受影响。"
            )

        return AttachmentSnapshot(
            filename=info["filename"],
            content_type=info["content_type"],
            size=len(data),
            sha256=digest,
            data=data,
        )
