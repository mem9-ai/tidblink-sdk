"""Organization Skills (``skills.ts`` in the TypeScript SDK).

The SDK returns the service's values unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from typing import Any, Literal, TypedDict

import httpx

from ._http import (
    Bytes,
    HttpClient,
    HttpResult,
    byte_view,
    pagination_query,
    require_creation_key,
    segment,
)
from .errors import OutcomeUnknownError, TiDBLinkError

__all__ = [
    "Skills",
    "SkillSummary",
    "SkillVersion",
    "SkillPage",
    "SkillDetail",
    "SkillUpload",
    "SkillUploadState",
    "SkillUploadValidation",
]


class SkillSummary(TypedDict):
    skillId: str
    name: str
    description: str
    revision: int
    #: The latest published version.
    headVersion: int
    createdAt: str
    updatedAt: str


class SkillVersion(TypedDict):
    version: int
    name: str
    description: str
    packageDigest: str
    rawSize: int
    extractedSize: int
    loadable: bool
    portable: bool
    createdAt: str


class SkillPage(TypedDict):
    skills: list[SkillSummary]
    nextCursor: str | None


class SkillDetail(TypedDict):
    """A Skill with one page of its published versions, newest first."""

    skill: SkillSummary
    versions: list[SkillVersion]
    nextCursor: str | None


SkillUploadState = Literal[
    "uploading",
    "pending",
    "uploaded",
    "fetching",
    "validating",
    "extracting",
    "awaiting_confirmation",
    "finalizing",
    "done",
    "failed",
    "aborted",
]


class SkillUploadValidation(TypedDict):
    entriesTotal: int | None
    fileCount: int | None
    entriesDone: int
    declaredBytes: int | None
    bytesDone: int
    diagnostics: list[dict[str, Any]]
    failureCode: str | None
    failurePath: str | None


class SkillUpload(TypedDict):
    """A Skill ZIP being validated and published.

    It reaches ``awaiting_confirmation`` once valid, then ``done`` with a
    ``version`` after publication; ``failed`` and ``aborted`` are final.
    """

    uploadId: str
    #: Every upload creates a new Skill.
    skillId: str
    state: SkillUploadState
    byteSize: int
    sha256: str
    #: Publish before this time.
    expiresAt: str | None
    validation: SkillUploadValidation
    #: The published version once ``state`` is ``done``.
    version: int | None
    createdAt: str
    updatedAt: str


_SETTLED: tuple[SkillUploadState, ...] = ("awaiting_confirmation", "done", "failed", "aborted")


def _upload_path(upload_id: str) -> str:
    return f"/skills/uploads/{segment(upload_id)}"


def _upload_receipt(result: HttpResult) -> SkillUpload:
    data = result.data
    upload = data.get("upload") if isinstance(data, dict) else None
    if not isinstance(upload, dict) or not isinstance(upload.get("uploadId"), str) or not upload["uploadId"]:
        raise OutcomeUnknownError(
            "Skill upload returned no usable state; repeat it with the same idempotency key",
            request_id=result.request_id,
        )
    return upload  # type: ignore[return-value]


def _wait_seconds(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number of seconds")
    if not 0 <= value < float("inf"):
        raise ValueError(f"{name} must be a non-negative number of seconds")
    return float(value)


class Skills:
    """Organization Skills that Templates can reference by ``skillId`` and ``version``."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def list(
        self, *, limit: int | None = None, cursor: str | None = None, request_id: str | None = None
    ) -> SkillPage:
        """Published, loadable Organization Skills."""
        return await self._http.get(f"/skills{pagination_query(limit, cursor)}", request_id=request_id)

    async def get(
        self,
        skill_id: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        request_id: str | None = None,
    ) -> SkillDetail:
        """A Skill and its published versions, newest first."""
        return await self._http.get(
            f"/skills/{segment(skill_id)}{pagination_query(limit, cursor)}", request_id=request_id
        )

    async def upload(self, *, data: Bytes, idempotency_key: str, request_id: str | None = None) -> SkillUpload:
        """Upload a Skill ZIP (at most 200 MiB) for validation. It is not published until :meth:`publish`.

        Repeating the same bytes with the same key returns the original upload.
        """
        view = byte_view(data, "data")
        if view.nbytes == 0:
            raise ValueError("Skill upload requires the ZIP bytes")
        require_creation_key(idempotency_key)
        # The request is retried, so it needs bytes that cannot change between
        # attempts: immutable bytes are sent as they are, others are copied once.
        content = data if isinstance(data, bytes) else view.tobytes()
        response, stable_request_id = await self._http.raw_response(
            "/skills/uploads",
            method="POST",
            content=content,
            headers={"content-type": "application/octet-stream"},
            idempotency_key=idempotency_key,
            retry="safe",
            outcome_unknown=True,
            request_id=request_id,
        )
        try:
            body = json.loads(await response.aread())
        except (httpx.TransportError, ValueError) as cause:
            raise OutcomeUnknownError(
                "Skill upload returned an unusable response; repeat it with the same idempotency key",
                request_id=stable_request_id,
                cause=cause,
            ) from cause
        finally:
            await response.aclose()
        return _upload_receipt(HttpResult(data=body, etag=None, request_id=stable_request_id))

    async def get_upload(self, upload_id: str, *, request_id: str | None = None) -> SkillUpload:
        response = await self._http.get(_upload_path(upload_id), request_id=request_id)
        return response["upload"]  # type: ignore[no-any-return]

    async def wait_for_upload(
        self,
        upload_id: str,
        *,
        until: Iterable[SkillUploadState] | None = None,
        timeout: float = 120.0,
        poll_interval: float = 1.0,
        request_id: str | None = None,
    ) -> SkillUpload:
        """Read the upload until it reaches a settled state.

        ``until`` defaults to ``awaiting_confirmation``, ``done``, ``failed``
        and ``aborted``. ``timeout`` is a local wait limit (default 120
        seconds); ``poll_interval`` is the delay between reads (default 1 second).
        """
        if isinstance(until, str):
            raise TypeError("until must be a collection of upload states")
        states = _SETTLED if until is None else tuple(until)
        timeout = _wait_seconds(timeout, "timeout")
        poll_interval = _wait_seconds(poll_interval, "poll_interval")
        deadline = time.monotonic() + timeout
        while True:
            upload = await self.get_upload(upload_id, request_id=request_id)
            if upload["state"] in states:
                return upload
            if time.monotonic() + poll_interval > deadline:
                raise TiDBLinkError(f"Skill upload is still {upload['state']}; read it again later")
            await asyncio.sleep(poll_interval)

    async def cancel_upload(self, upload_id: str, *, request_id: str | None = None) -> None:
        """Cancel an upload that is not published."""
        await self._http.delete_response(_upload_path(upload_id), outcome_unknown=True, request_id=request_id)

    async def publish(self, upload_id: str, *, idempotency_key: str, request_id: str | None = None) -> SkillUpload:
        """Publish a validated upload as the Skill's next version.

        Publication finishes asynchronously: wait for ``done`` to read the
        ``version``. Repeating publication with the same key returns the same upload.
        """
        require_creation_key(idempotency_key)
        return _upload_receipt(
            await self._http.post_response(
                f"{_upload_path(upload_id)}/publish",
                idempotency_key=idempotency_key,
                idempotent=True,
                outcome_unknown=True,
                request_id=request_id,
            )
        )
