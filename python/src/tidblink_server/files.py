"""End User files: uploads, listing and verified downloads (``files.ts`` in the TypeScript SDK)."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, NotRequired, TypedDict
from urllib.parse import urljoin, urlsplit

import httpx

from ._http import (
    utf16_length,
    Bytes,
    HttpClient,
    byte_view,
    bytes_request,
    is_byte_size,
    is_unsent_failure,
    mark_unsent,
    media_type,
    query_string,
    require_hashed_length,
    segment,
    unsent_body,
)
from .errors import (
    OutcomeUnknownError,
    TiDBLinkApiError,
    TiDBLinkConnectionError,
    TiDBLinkError,
)

__all__ = [
    "UserFile",
    "UserFileSource",
    "FileUploadCapabilities",
    "FileUpload",
    "FileUploadGrant",
    "FileUploadStatus",
    "FileUploadPart",
    "UploadedFilePart",
    "FilePage",
    "FileDownloadGrant",
    "FileBytes",
    "FileDownload",
    "FileContent",
    "FileUploadError",
    "FileUploadOutcomeUnknownError",
    "UserFiles",
]


class UserFileSource(TypedDict):
    """Where a file came from.

    ``{"kind": "user_upload"}`` for an upload by the owner;
    ``{"kind": "session", "sessionId", "operationId"}`` for a file an
    Operation of the owner's Session produced, such as an image or a binary
    MCP tool result.
    """

    kind: Literal["user_upload", "session"]
    sessionId: NotRequired[str]
    operationId: NotRequired[str]


class UserFile(TypedDict):
    fileId: str
    originalName: str
    status: Literal["uploading", "ready", "failed"]
    byteSize: int | None
    sha256: str | None
    checksumVerification: Literal["whole", "part_only"] | None
    contentType: str | None
    lastError: str | None
    createdAt: str
    updatedAt: str
    source: UserFileSource


class FileUploadCapabilities(TypedDict):
    protocol: Literal["tidb-link.user-files.v2"]
    inlineThreshold: int
    maxUploadBytes: int
    checksum: Literal["sha-256"]


class FileUploadGrant(TypedDict):
    method: Literal["PUT"]
    url: str
    headers: dict[str, str]
    expiresAt: str


class FileUpload(TypedDict):
    uploadId: str
    file: UserFile
    mode: Literal["tidb-link", "s3"]
    expectedChecksumVerification: Literal["whole"]
    expiresAt: str
    contentUrl: NotRequired[str]
    uploadUrl: NotRequired[FileUploadGrant]
    partSize: NotRequired[int]
    totalParts: NotRequired[int]


class FileUploadStatus(TypedDict):
    uploadId: str
    file: UserFile
    mode: Literal["tidb-link", "s3", "initializing"]
    state: Literal["active", "completing", "aborting", "completed", "aborted"]
    expiresAt: str


class FileUploadPart(TypedDict):
    number: int
    url: str
    size: int
    headers: dict[str, str]
    expiresAt: str


class UploadedFilePart(TypedDict):
    number: int
    etag: str
    sha256: str


class FilePage(TypedDict):
    files: list[UserFile]
    nextCursor: str | None


class FileDownloadGrant(TypedDict):
    url: str
    expiresAt: str
    byteSize: int
    sha256: str


@dataclass(frozen=True)
class FileBytes:
    """File content the service returned directly."""

    data: bytes
    content_type: str
    type: Literal["bytes"] = field(default="bytes", init=False)


@dataclass(frozen=True)
class FileDownload:
    """A short-lived download grant; pass it to :meth:`UserFiles.download_from_grant`."""

    grant: FileDownloadGrant
    type: Literal["download"] = field(default="download", init=False)


FileContent = FileBytes | FileDownload


class FileUploadError(TiDBLinkError):
    """``upload()`` failed after it created the upload, in a byte transfer that sent no body bytes.

    The transport failed before reading the body, or the bytes changed length
    after they were hashed. ``cause`` is the original error.
    """

    idempotency_key: str
    upload_id: str

    def __init__(self, idempotency_key: str, upload_id: str, cause: BaseException) -> None:
        super().__init__(
            "File upload failed after the upload was created; read the upload before deciding what to repeat",
            request_id=cause.request_id if isinstance(cause, TiDBLinkError) else None,
            cause=cause,
        )
        self.idempotency_key = idempotency_key
        self.upload_id = upload_id


class FileUploadOutcomeUnknownError(OutcomeUnknownError):
    """``upload()`` may have had an effect; recover the original upload before transferring again."""

    idempotency_key: str
    upload_id: str | None

    def __init__(
        self, idempotency_key: str, upload_id: str | None, cause: OutcomeUnknownError
    ) -> None:
        super().__init__(
            "File upload outcome is unknown; recover the original upload before transferring again",
            request_id=cause.request_id,
            cause=cause,
        )
        self.idempotency_key = idempotency_key
        self.upload_id = upload_id


_SHA256_HEX = re.compile(r"[a-f0-9]{64}")
_UPLOAD_KEY = re.compile(r"[\x21-\x7e]{1,255}")
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_HEADER_VALUE = re.compile(r"[\t\x20-\x7e]*")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_DOWNLOAD_GRANT_TYPE = "application/vnd.tidb-link.file-download+json"


class _RedirectRefused(Exception):
    pass


@dataclass(frozen=True)
class _Checksum:
    hex: str
    base64: str
    #: The number of bytes hashed.
    byte_length: int


def _digest(view: memoryview) -> _Checksum:
    """SHA-256 of the bytes, read in place."""
    value = hashlib.sha256(view).digest()
    return _Checksum(value.hex(), base64.b64encode(value).decode("ascii"), view.nbytes)


def _file_bytes(data: object) -> memoryview:
    return byte_view(data, "File bytes")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_HEX.fullmatch(value))


def parse_time(value: object) -> datetime | None:
    """An ISO 8601 time from the service, as an aware datetime, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    return value


def _upload_path(upload_id: str) -> str:
    return f"/drive/uploads/{segment(_require_id(upload_id, 'upload_id'))}"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name:
            return value
    return None


def _file_receipt(value: Any, request_id: str) -> UserFile:
    file = value.get("file") if isinstance(value, dict) else None
    if (
        not isinstance(file, dict)
        or not file.get("fileId")
        or file.get("status") not in ("uploading", "ready", "failed")
    ):
        raise OutcomeUnknownError(
            "File operation returned no usable File; read the upload state", request_id=request_id
        )
    return file  # type: ignore[return-value]


def _require_ready(file: UserFile, byte_size: int, sha256: str) -> UserFile:
    if (
        file.get("status") != "ready"
        or file.get("byteSize") != byte_size
        or file.get("sha256") != sha256
        or file.get("checksumVerification") != "whole"
    ):
        raise TiDBLinkError("File is not ready with the expected complete size and checksum")
    return file


def _require_active_grant(expires_at: object) -> None:
    expiry = parse_time(expires_at)
    if expiry is None or expiry <= datetime.now(timezone.utc):
        raise TiDBLinkApiError(
            status=410,
            code="file_transfer_expired",
            message="File transfer credential expired; obtain a fresh credential for the original file",
        )


def _transfer_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or "#" in url
    ):
        raise ValueError(
            "File transfer URL must be HTTP(S) without embedded credentials or a fragment"
        )
    return url


def _grant_headers(headers: object) -> dict[str, str]:
    """A grant's own headers, which are all a transfer carries; ``content-length`` comes from the hashed bytes."""
    if not isinstance(headers, Mapping):
        raise TypeError("File transfer headers must be a mapping")
    result: dict[str, str] = {}
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not _HEADER_NAME.fullmatch(name)
            or not _HEADER_VALUE.fullmatch(value)
        ):
            raise ValueError("File transfer headers must be valid HTTP header names and values")
        if name.lower() != "content-length":
            result[name] = value
    return result


def _require_part(part: object) -> FileUploadPart:
    if (
        not isinstance(part, Mapping)
        or not _is_int(part.get("number"))
        or not _is_int(part.get("size"))
        or not isinstance(part.get("url"), str)
        or not isinstance(part.get("headers"), Mapping)
        or not isinstance(part.get("expiresAt"), str)
    ):
        raise TypeError("upload_part requires a FileUploadPart from presign_part")
    return part  # type: ignore[return-value]


async def fetch_download(http: HttpClient, url: str) -> httpx.Response:
    """GET a download URL, which carries its own authority, without the API Key, still streaming.

    One HTTPS redirect is followed. The caller reads and closes the response.
    """
    destination = _transfer_url(str(http.resolve_url(url)))
    client = http.client
    try:
        response = await client.send(
            httpx.Request("GET", destination), stream=True, auth=None, follow_redirects=False
        )
        if response.status_code in _REDIRECTS:
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise TiDBLinkError("File download redirect omitted its destination")
            destination = _transfer_url(urljoin(destination, location))
            if urlsplit(destination).scheme != "https":
                raise TiDBLinkError("File download redirect requires HTTPS")
            response = await client.send(
                httpx.Request("GET", destination),
                stream=True,
                auth=None,
                follow_redirects=False,
            )
            if response.is_redirect:
                await response.aclose()
                raise _RedirectRefused("File download redirected twice")
    except TiDBLinkError:
        raise
    except Exception:
        raise TiDBLinkConnectionError("File download could not be reached") from None
    return response


class UserFiles:
    """One End User's files: uploads, listing and verified downloads."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def capabilities(self, *, request_id: str | None = None) -> FileUploadCapabilities:
        return (await self._http.get("/drive/upload-capabilities", request_id=request_id))[
            "capabilities"
        ]

    async def create_upload(
        self,
        *,
        idempotency_key: str,
        original_name: str,
        byte_size: int,
        sha256: str,
        content_type: str | None,
        request_id: str | None = None,
    ) -> FileUpload:
        """Create an upload for a file of ``byte_size`` bytes with this complete SHA-256.

        Persist ``idempotency_key`` with the original input before calling;
        reuse it only for that exact file.
        """
        message = "File upload requires a stable idempotency_key (1-255 visible ASCII characters)"
        if not isinstance(idempotency_key, str):
            raise TypeError(message)
        if not _UPLOAD_KEY.fullmatch(idempotency_key):
            raise ValueError(message)
        message = (
            "File upload requires a name, nonnegative byte_size, complete SHA-256 and nullable content_type"
        )
        if (
            not isinstance(original_name, str)
            or not _is_int(byte_size)
            or not isinstance(sha256, str)
            or not (content_type is None or isinstance(content_type, str))
        ):
            raise TypeError(message)
        if (
            not original_name
            or utf16_length(original_name) > 255
            or byte_size < 0
            or not _SHA256_HEX.fullmatch(sha256)
            or (content_type is not None and utf16_length(content_type) > 255)
        ):
            raise ValueError(message)
        result = await self._http.post_response(
            "/drive/uploads",
            {
                "originalName": original_name,
                "byteSize": byte_size,
                "sha256": sha256,
                "contentType": content_type,
            },
            idempotency_key=idempotency_key,
            outcome_unknown=True,
            request_id=request_id,
        )
        upload = result.data.get("upload") if isinstance(result.data, dict) else None
        if (
            not isinstance(upload, dict)
            or not upload.get("uploadId")
            or not isinstance(upload.get("file"), dict)
            or upload["file"].get("fileId") != upload["uploadId"]
            or upload.get("mode") not in ("tidb-link", "s3")
            or parse_time(upload.get("expiresAt")) is None
            or upload.get("expectedChecksumVerification") != "whole"
        ):
            raise OutcomeUnknownError(
                "File creation returned no usable upload; replay the same creation identity",
                request_id=result.request_id,
            )
        return upload  # type: ignore[return-value]

    async def get_upload(self, upload_id: str, *, request_id: str | None = None) -> FileUploadStatus:
        return (await self._http.get(_upload_path(upload_id), request_id=request_id))["upload"]

    async def upload_content(
        self, upload_id: str, data: Bytes, *, request_id: str | None = None
    ) -> UserFile:
        """Send the whole file of an inline upload.

        The SDK hashes and sends ``data`` without copying it, so leave it
        unchanged until the call settles.
        """
        view = _file_bytes(data)
        return await self._send_content(upload_id, view, _digest(view), request_id)

    async def _send_content(
        self, upload_id: str, view: memoryview, checksum: _Checksum, request_id: str | None
    ) -> UserFile:
        path = f"{_upload_path(upload_id)}/content"
        body, headers = bytes_request(
            view,
            {
                "content-type": "application/octet-stream",
                "content-digest": f"sha-256=:{checksum.base64}:",
            },
            checksum.byte_length,
        )
        response, stable_request_id = await self._http.raw_response(
            path,
            method="PUT",
            content=body,
            headers=headers,
            request_id=request_id,
            outcome_unknown=True,
        )
        try:
            value = json.loads(await response.aread())
            return _file_receipt(value, stable_request_id)
        except OutcomeUnknownError:
            raise
        except Exception as cause:
            raise OutcomeUnknownError(
                "Inline transfer returned no usable File; read the upload state",
                request_id=stable_request_id,
                cause=cause,
            ) from cause
        finally:
            await response.aclose()

    async def presign_part(
        self, upload_id: str, number: int, sha256: str, *, request_id: str | None = None
    ) -> FileUploadPart:
        """Sign the transfer of one part with the SHA-256 of its bytes."""
        path = f"{_upload_path(upload_id)}/parts"
        if not _is_int(number) or not isinstance(sha256, str):
            raise TypeError("A part requires a positive number and SHA-256")
        if number < 1 or not _SHA256_HEX.fullmatch(sha256):
            raise ValueError("A part requires a positive number and SHA-256")
        result = await self._http.post_response(
            f"{path}/{number}/presign",
            {"sha256": sha256},
            outcome_unknown=True,
            request_id=request_id,
        )
        part = result.data.get("part") if isinstance(result.data, dict) else None
        if (
            not isinstance(part, dict)
            or part.get("number") != number
            or not _is_int(part.get("size"))
            or part["size"] < 0
            or not part.get("url")
            or not isinstance(part.get("url"), str)
            or not isinstance(part.get("headers"), dict)
            or parse_time(part.get("expiresAt")) is None
        ):
            raise OutcomeUnknownError(
                "Part signing returned no usable grant; reconcile before transferring",
                request_id=result.request_id,
            )
        return part  # type: ignore[return-value]

    async def upload_part(self, part: FileUploadPart, data: Bytes) -> UploadedFilePart:
        """Send one part to its presigned URL, which carries its own authority.

        The SDK hashes and sends ``data`` without copying it, so leave it
        unchanged until the call settles.
        """
        view = _file_bytes(data)
        return await self._send_part(_require_part(part), view, _digest(view))

    async def _send_part(
        self, part: FileUploadPart, view: memoryview, checksum: _Checksum
    ) -> UploadedFilePart:
        if checksum.byte_length != part["size"]:
            raise ValueError("Part bytes do not match the signed size")
        headers = _grant_headers(part["headers"])
        if _header(headers, "x-amz-checksum-sha256") != checksum.base64:
            raise ValueError("Part bytes do not match the signed checksum")
        response_headers = await self._transfer(
            part["url"], part["expiresAt"], view, headers, checksum.byte_length
        )
        etag = response_headers.get("etag")
        if not etag:
            raise OutcomeUnknownError("Part transfer returned no ETag; reconcile before completing")
        return {"number": part["number"], "etag": etag, "sha256": checksum.hex}

    async def _transfer(
        self,
        url: str,
        expires_at: str,
        view: memoryview,
        headers: dict[str, str],
        length: int,
    ) -> httpx.Headers:
        """PUT bytes to a transfer grant and return the response headers.

        The request carries only the grant's headers: never the API Key, and
        none of the transport's default headers, cookies or auth.
        """
        _require_active_grant(expires_at)
        destination = _transfer_url(str(self._http.resolve_url(url)))
        body, request_headers = bytes_request(view, headers, length)
        client = self._http.client
        try:
            request = httpx.Request("PUT", destination, headers=request_headers, content=body)
            response = await client.send(request, stream=True, auth=None, follow_redirects=False)
            if response.is_redirect:
                await response.aclose()
                raise _RedirectRefused("File transfer answered with a redirect, which is refused")
        except Exception as cause:
            unsent = unsent_body(body)
            if isinstance(unsent, BaseException):
                raise unsent from cause
            # The service cannot have received a transfer with no body bytes.
            if unsent:
                raise mark_unsent(
                    TiDBLinkConnectionError("File transfer could not be sent", cause=cause)
                ) from cause
            raise OutcomeUnknownError(
                "File transfer outcome is unknown; inspect the original upload", cause=cause
            ) from cause
        except BaseException as cause:
            # Cancellation propagates unchanged; it is only marked when nothing was sent.
            unsent = unsent_body(body)
            if isinstance(unsent, BaseException) and unsent is not cause:
                raise unsent from cause
            if unsent == "unsent":
                mark_unsent(cause)
            raise
        await response.aclose()
        if not response.is_success:
            if response.status_code >= 500 or response.status_code == 408:
                raise OutcomeUnknownError(
                    "File transfer outcome is unknown; inspect the original upload"
                )
            raise TiDBLinkApiError(
                status=response.status_code,
                code="file_transfer_failed",
                message="File byte transfer was refused",
            )
        return response.headers

    async def complete_upload(
        self,
        upload_id: str,
        parts: Sequence[UploadedFilePart] | None = None,
        *,
        request_id: str | None = None,
    ) -> UserFile:
        """Complete an upload; a multipart upload names every part it transferred."""
        path = f"{_upload_path(upload_id)}/complete"
        result = await self._http.post_response(
            path,
            {} if parts is None else {"parts": list(parts)},
            outcome_unknown=True,
            request_id=request_id,
        )
        return _file_receipt(result.data, result.request_id)

    async def cancel_upload(self, upload_id: str, *, request_id: str | None = None) -> UserFile:
        result = await self._http.delete_response(
            _upload_path(upload_id), outcome_unknown=True, request_id=request_id
        )
        return _file_receipt(result.data, result.request_id)

    async def upload(
        self,
        *,
        idempotency_key: str,
        original_name: str,
        data: Bytes,
        content_type: str | None = None,
        request_id: str | None = None,
    ) -> UserFile:
        """Upload a whole file, inline or in parts as the service plans, and confirm it is ready.

        Persist ``idempotency_key`` with the original input before calling;
        reuse it only for that exact file. The SDK hashes and sends ``data``
        without copying it, so leave it unchanged until the upload settles.
        """
        view = _file_bytes(data)
        checksum = _digest(view)
        upload_id: str | None = None
        try:
            upload = await self.create_upload(
                idempotency_key=idempotency_key,
                original_name=original_name,
                byte_size=checksum.byte_length,
                sha256=checksum.hex,
                content_type=content_type,
                request_id=request_id,
            )
            upload_id = upload["uploadId"]
            if upload["file"].get("status") == "ready":
                return _require_ready(upload["file"], checksum.byte_length, checksum.hex)
            _require_active_grant(upload["expiresAt"])
            parts: list[UploadedFilePart] | None = None
            if upload["mode"] == "tidb-link":
                grant = upload.get("uploadUrl")
                if grant:
                    if (
                        not isinstance(grant, dict)
                        or grant.get("method") != "PUT"
                        or not isinstance(grant.get("headers"), Mapping)
                        or _header(grant["headers"], "content-digest")
                        != f"sha-256=:{checksum.base64}:"
                    ):
                        raise TiDBLinkError(
                            "Inline upload grant does not match the complete file checksum"
                        )
                    await self._transfer(
                        grant["url"],
                        grant["expiresAt"],
                        view,
                        _grant_headers(grant["headers"]),
                        checksum.byte_length,
                    )
                else:
                    await self._send_content(upload_id, view, checksum, request_id)
            else:
                part_size = upload.get("partSize")
                total_parts = upload.get("totalParts")
                if (
                    not _is_int(part_size)
                    or part_size <= 0  # type: ignore[operator]
                    or not _is_int(total_parts)
                    or total_parts != math.ceil(checksum.byte_length / part_size)  # type: ignore[operator]
                ):
                    raise TiDBLinkError("Multipart upload plan does not cover the complete file")
                assert isinstance(part_size, int) and isinstance(total_parts, int)
                parts = []
                for number in range(1, total_parts + 1):
                    require_hashed_length(view, checksum.byte_length)
                    part_view = view[(number - 1) * part_size : number * part_size]
                    part_checksum = _digest(part_view)
                    part = await self.presign_part(
                        upload_id, number, part_checksum.hex, request_id=request_id
                    )
                    parts.append(await self._send_part(part, part_view, part_checksum))
            return _require_ready(
                await self.complete_upload(upload_id, parts, request_id=request_id),
                checksum.byte_length,
                checksum.hex,
            )
        except OutcomeUnknownError as cause:
            raise FileUploadOutcomeUnknownError(idempotency_key, upload_id, cause) from cause
        except Exception as cause:
            if upload_id is not None and is_unsent_failure(cause):
                raise FileUploadError(idempotency_key, upload_id, cause) from cause
            raise

    async def list(
        self,
        *,
        history: bool | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        request_id: str | None = None,
    ) -> FilePage:
        """List the User's files; ``history`` includes files that are not ready."""
        if history is not None and not isinstance(history, bool):
            raise TypeError("history must be a boolean")
        if limit is not None and not _is_int(limit):
            raise TypeError("limit must be an integer")
        if cursor is not None and not isinstance(cursor, str):
            raise TypeError("cursor must be text")
        query = query_string(
            {
                "history": None if history is None else ("true" if history else "false"),
                "limit": limit,
                "cursor": cursor,
            }
        )
        return await self._http.get(f"/files{query}", request_id=request_id)

    async def get(self, file_id: str, *, request_id: str | None = None) -> UserFile:
        path = f"/files/{segment(_require_id(file_id, 'file_id'))}"
        return (await self._http.get(path, request_id=request_id))["file"]

    async def get_content(self, file_id: str, *, request_id: str | None = None) -> FileContent:
        """The file's bytes, as :class:`FileBytes`, or a :class:`FileDownload` grant to fetch them from."""
        path = f"/files/{segment(_require_id(file_id, 'file_id'))}/content"
        response, stable_request_id = await self._http.raw_response(path, request_id=request_id)
        content_type = response.headers.get("content-type") or "application/octet-stream"
        try:
            if media_type(response) == _DOWNLOAD_GRANT_TYPE:
                grant = json.loads(await response.aread())
                if (
                    not isinstance(grant, dict)
                    or not grant.get("url")
                    or not isinstance(grant["url"], str)
                    or not is_byte_size(grant.get("byteSize"))
                    or not _is_sha256(grant.get("sha256"))
                    or parse_time(grant.get("expiresAt")) is None
                ):
                    raise TiDBLinkError(
                        "File content returned no usable download grant", request_id=stable_request_id
                    )
                return FileDownload(grant)  # type: ignore[arg-type]
            return FileBytes(await response.aread(), content_type)
        except TiDBLinkError:
            raise
        except Exception as cause:
            raise TiDBLinkConnectionError(
                "File content could not be read completely", request_id=stable_request_id
            ) from cause
        finally:
            await response.aclose()

    async def download_from_grant(self, grant: FileDownloadGrant) -> bytes:
        """Download from a grant, which carries its own authority, and verify size and SHA-256.

        One HTTPS redirect is followed, without the API Key.
        """
        if not isinstance(grant, Mapping) or not isinstance(grant.get("url"), str):
            raise TypeError("download_from_grant requires a FileDownloadGrant")
        _require_active_grant(grant.get("expiresAt"))
        response = await fetch_download(self._http, grant["url"])
        try:
            if not response.is_success:
                raise TiDBLinkApiError(
                    status=response.status_code,
                    code="file_download_failed",
                    message="File download was refused; obtain a fresh credential if it expired",
                )
            try:
                data = await response.aread()
            except Exception:
                raise TiDBLinkConnectionError(
                    "File download ended before the complete body arrived"
                ) from None
        finally:
            await response.aclose()
        if len(data) != grant.get("byteSize") or hashlib.sha256(data).hexdigest() != grant.get(
            "sha256"
        ):
            raise TiDBLinkError("Downloaded file does not match its complete size and checksum")
        return data

    async def download(self, file_id: str, *, request_id: str | None = None) -> bytes:
        """Download a ready, whole-file verified File and verify its size and SHA-256."""
        file = await self.get(file_id, request_id=request_id)
        if (
            file.get("status") != "ready"
            or file.get("byteSize") is None
            or file.get("sha256") is None
            or file.get("checksumVerification") != "whole"
        ):
            raise TiDBLinkError("Only a ready, whole-file verified File can be downloaded")
        content = await self.get_content(file_id, request_id=request_id)
        if isinstance(content, FileDownload):
            if (
                content.grant["byteSize"] != file["byteSize"]
                or content.grant["sha256"] != file["sha256"]
            ):
                raise TiDBLinkError("Download grant does not match the selected File")
            return await self.download_from_grant(content.grant)
        if (
            len(content.data) != file["byteSize"]
            or hashlib.sha256(content.data).hexdigest() != file["sha256"]
        ):
            raise TiDBLinkError("Downloaded file does not match its complete size and checksum")
        return content.data
