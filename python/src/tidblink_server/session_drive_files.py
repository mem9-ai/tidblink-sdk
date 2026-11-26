"""Files in a Session's Drive, read by path (``session-drive-files.ts`` in the TypeScript SDK)."""

from __future__ import annotations

import json
from typing import Any, TypedDict

import httpx

from ._http import HttpClient, api_error_from, is_byte_size, media_type, query_string, segment
from .errors import TiDBLinkConnectionError, TiDBLinkError
from .files import fetch_download, parse_time

__all__ = ["SessionDriveFiles", "SessionDriveFileTicket"]

_DOWNLOAD_TICKET_TYPE = "application/vnd.tidb-link.file-download+json"


class SessionDriveFileTicket(TypedDict):
    """A short-lived download ticket for one file in a Session's Drive.

    Its holder, such as a device, downloads the file from ``url`` without an
    API Key until ``expiresAt``. It is a bearer credential: do not log or
    store it.
    """

    #: Absolute URL, resolved against the client's ``base_url``.
    url: str
    expiresAt: str
    byteSize: int


def _validate_file_read(path: object, version: object) -> None:
    """Check a read's or ticket's path and version before anything is sent."""
    if not isinstance(path, str):
        raise TypeError("A Session file path must be text")
    if not path:
        raise ValueError("A Session file path must not be empty")
    # The version is opaque: its form is the service's to judge.
    if version is not None:
        if not isinstance(version, str):
            raise TypeError("A Session file version must be text")
        if not version:
            raise ValueError("A Session file version must be the non-empty text a File Notification announced")


async def _bytes_of(response: httpx.Response, request_id: str) -> bytes:
    try:
        return await response.aread()
    except Exception:
        raise TiDBLinkConnectionError(
            "Session file download ended before the complete body arrived", request_id=request_id
        ) from None
    finally:
        await response.aclose()


class SessionDriveFiles:
    """Files in one Session's Drive, read by path, such as the path a File Notification announced.

    Reads use the authority of writing Session files.
    """

    def __init__(self, http: HttpClient, session_id: str) -> None:
        self._http = http
        self._path = f"/sessions/{segment(session_id)}/files"

    async def read(self, path: str, *, version: str | None = None, request_id: str | None = None) -> bytes:
        """The file's bytes, held in memory.

        With ``version``, the version a File Notification announced, only that
        version is read, and a file changed since then raises
        :class:`DriveFileVersionConflictError`. Without it, the current bytes
        are read. A file above the service's inline limit (50 MiB) is
        downloaded through the ticket the service answers with.
        """
        _validate_file_read(path, version)
        query = query_string({"path": path, "version": version})
        response, stable_request_id = await self._http.raw_response(
            f"{self._path}/content{query}",
            headers={"accept": "*/*"},
            request_id=request_id,
            retry="safe",
        )
        if media_type(response) != _DOWNLOAD_TICKET_TYPE:
            return await _bytes_of(response, stable_request_id)
        try:
            value: Any = json.loads(await response.aread())
        except Exception:
            value = None
        finally:
            await response.aclose()
        # The service enforces the ticket's expiry; a client clock may be wrong.
        ticket = self._ticket(value, stable_request_id)
        download = await fetch_download(self._http, ticket["url"])
        if not download.is_success:
            try:
                raise await api_error_from(download, stable_request_id)
            finally:
                await download.aclose()
        data = await _bytes_of(download, stable_request_id)
        if len(data) != ticket["byteSize"]:
            raise TiDBLinkError(
                "Downloaded Session file does not match its ticket's size", request_id=stable_request_id
            )
        return data

    async def create_download_ticket(
        self, path: str, *, version: str | None = None, request_id: str | None = None
    ) -> SessionDriveFileTicket:
        """A 60-second download ticket for the file, to hand to a device or another process that holds no API Key.

        Pinned to ``version``, its download fails with a version conflict once
        the file has changed.
        """
        _validate_file_read(path, version)
        body: dict[str, str] = {"path": path}
        if version is not None:
            body["version"] = version
        result = await self._http.post_response(
            f"{self._path}/download-tickets", body, request_id=request_id
        )
        return self._ticket(result.data, result.request_id)

    def _ticket(self, value: Any, request_id: str) -> SessionDriveFileTicket:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("url"), str)
            or not value["url"]
            or not is_byte_size(value.get("byteSize"))
            or parse_time(value.get("expiresAt")) is None
        ):
            raise TiDBLinkError("Session file returned no usable download ticket", request_id=request_id)
        return {
            "url": self._http.resolve_url(value["url"]),
            "expiresAt": value["expiresAt"],
            "byteSize": value["byteSize"],
        }
