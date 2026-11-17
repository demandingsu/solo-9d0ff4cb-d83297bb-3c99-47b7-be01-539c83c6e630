"""Capture orchestration: validate, compare with archive, commit atomically."""
from __future__ import annotations

import logging

from .domain import (
    ArchiveError,
    CaptureResult,
    RevisionMismatch,
    SourceInconsistent,
    SourceNotFound,
    SourceSnapshot,
    SourceUnavailable,
    SourceWithdrawn,
    VersionRecord,
    normalize_number,
    utc_now_iso,
)
from .source import HttpSourceClient
from .store import ArchiveStore

log = logging.getLogger(__name__)


class CaptureService:
    def __init__(self, store: ArchiveStore, source: HttpSourceClient):
        self.store = store
        self.source = source

    def capture(self, raw_number: str) -> CaptureResult:
        number = normalize_number(raw_number)

        try:
            snapshot = self.source.fetch(number)
        except ArchiveError as exc:
            self._record_failure(number, exc)
            raise

        existing = self.store.latest_version(number)
        status = self.store.get_announcement(number)

        if existing is None:
            return self._commit(snapshot, outcome="created", detail="首次采集")

        if snapshot.revision < existing.revision:
            result = CaptureResult(
                status="conflict",
                number=number,
                revision=snapshot.revision,
                message=(
                    f"源站修订号回退（档案已有 r{existing.revision}，"
                    f"源站现在为 r{snapshot.revision}），未创建或覆盖任何版本。"
                ),
            )
            self._log(number, result, detail=result.message)
            return result

        if snapshot.revision == existing.revision:
            if snapshot.content_hash == existing.content_hash:
                archived_retracted = bool(
                    status and status["source_retracted"]
                )
                if snapshot.retracted != archived_retracted:
                    self.store.update_source_retracted(
                        number, snapshot.revision, snapshot.retracted
                    )
                    return CaptureResult(
                        status="retracted_update",
                        number=number,
                        revision=snapshot.revision,
                        message=(
                            "同一修订号下撤稿状态发生变化，已更新状态，"
                            "未新建版本，旧版仍可下载。"
                        ),
                    )
                return CaptureResult(
                    status="unchanged",
                    number=number,
                    revision=snapshot.revision,
                    message=(
                        f"修订 r{snapshot.revision} 已存档且内容一致，"
                        "未重复建版。"
                    ),
                )

            result = CaptureResult(
                status="conflict",
                number=number,
                revision=snapshot.revision,
                message=(
                    f"源站冲突：修订号 r{snapshot.revision} 已存档，"
                    "但本次抓取的内容与存档不同。已拒收本次采集，"
                    "旧版保持原样，未覆盖。"
                ),
                details={
                    "archived_hash": existing.content_hash,
                    "source_hash": snapshot.content_hash,
                },
            )
            self._log(
                number,
                result,
                detail=(
                    result.message
                    + f" 存档指纹 {existing.content_hash[:16]}…，"
                    f"源站指纹 {snapshot.content_hash[:16]}…"
                ),
            )
            return result

        # snapshot.revision > existing.revision: a genuinely new revision.
        detail = (
            f"r{existing.revision} → r{snapshot.revision}"
            + ("（源站已撤稿）" if snapshot.retracted else "")
        )
        return self._commit(snapshot, outcome="created", detail=detail)

    def _commit(
        self, snapshot: SourceSnapshot, *, outcome: str, detail: str
    ) -> CaptureResult:
        record = VersionRecord.from_snapshot(snapshot, utc_now_iso())
        inserted = self.store.insert_version(record, outcome=outcome, detail=detail)
        if not inserted:
            # Another capture claimed this revision between our read and write.
            # Never assume it matched: compare what actually landed.
            winner = self.store.get_version(snapshot.number, snapshot.revision)
            if winner is not None and winner.content_hash != record.content_hash:
                message = (
                    f"并发采集冲突：修订号 r{snapshot.revision} 已被另一次采集"
                    "以不同内容存档。本次未建版、未覆盖；如需核对，请检查源站。"
                )
                self.store.touch_checked(
                    snapshot.number, "conflict", message, snapshot.revision
                )
                return CaptureResult(
                    status="conflict",
                    number=snapshot.number,
                    revision=snapshot.revision,
                    message=message,
                    details={
                        "archived_hash": winner.content_hash,
                        "source_hash": record.content_hash,
                    },
                )
            return CaptureResult(
                status="unchanged",
                number=snapshot.number,
                revision=snapshot.revision,
                message=(
                    f"修订 r{snapshot.revision} 已被并发采集存档且内容一致，"
                    "本次未重复建版。"
                ),
            )
        if snapshot.retracted:
            message = (
                f"已存档撤稿状态 r{snapshot.revision}，正文与附件均可查，"
                "此前版本仍可下载。"
            )
        else:
            message = f"已存档 r{snapshot.revision} 的正文与附件。"
        return CaptureResult(
            status="created",
            number=snapshot.number,
            revision=snapshot.revision,
            message=message,
        )

    def _record_failure(self, number: str, exc: ArchiveError) -> None:
        outcome = {
            SourceNotFound: "not_found",
            SourceWithdrawn: "withdrawn",
            SourceUnavailable: "source_unavailable",
            RevisionMismatch: "revision_mismatch",
            SourceInconsistent: "source_inconsistent",
        }.get(type(exc), "rejected")
        try:
            # A failed capture is still a check; log it without touching
            # versions. Unknown numbers get no announcement row.
            if self.store.announcement_exists(number):
                self.store.touch_checked(number, outcome, exc.message, None)
            else:
                self.store.log_event(number, outcome, exc.message, None)
        except ArchiveError:
            log.exception("failed to record capture failure for %s", number)

    def _log(self, number: str, result: CaptureResult, *, detail: str) -> None:
        self.store.touch_checked(
            number, result.status, detail, result.revision
        )
