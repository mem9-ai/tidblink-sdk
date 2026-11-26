"""Port of ``typescript/test/session-drive-files.test.mjs``."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from support import BASE_URL, ORGANIZATION_API_KEY, Service, error_response, json_response, serve
from tidblink_server import (
    ConflictError,
    DriveFileVersionConflictError,
    TiDBLinkApiError,
    TiDBLinkError,
)

SESSION_PATH = "/api/v1/org/users/user_1/sessions/sess_1"
TICKET_TYPE = "application/vnd.tidb-link.file-download+json"


def refuse(status: int, code: str) -> httpx.Response:
    response = error_response(status, code, f"refused: {code}")
    response.headers["x-request-id"] = f"req-{code}"
    return response


def files_of(handler: Any) -> tuple[Any, Service]:
    org, service = serve(handler)
    return org.user("user_1").session("sess_1").files, service


async def test_read_returns_the_bytes_of_a_file_in_the_sessions_drive_pinned_to_the_announced_version() -> None:
    files, service = files_of(
        lambda request, _: httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"firmware")
    )
    assert await files.read("build/firmware v2.bin", version="v1_AAAA-BBBB") == b"firmware"
    await files.read("notes.md")
    assert [(call.method, call.path, call.headers["authorization"]) for call in service.calls] == [
        (
            "GET",
            f"{SESSION_PATH}/files/content?path=build%2Ffirmware+v2.bin&version=v1_AAAA-BBBB",
            f"Bearer {ORGANIZATION_API_KEY}",
        ),
        ("GET", f"{SESSION_PATH}/files/content?path=notes.md", f"Bearer {ORGANIZATION_API_KEY}"),
    ]


async def test_a_changed_file_raises_drive_file_version_conflict_error_distinct_from_a_missing_file_or_session() -> None:
    def handler(request: httpx.Request, _: Service) -> httpx.Response:
        path = request.url.params.get("path")
        if path == "changed.bin":
            return refuse(409, "drive_file_version_conflict")
        if path == "gone.bin":
            return refuse(404, "drive_file_not_found")
        if path == "stale.json":
            return refuse(409, "other_conflict")
        return refuse(404, "session_not_found")

    files, _ = files_of(handler)
    with pytest.raises(DriveFileVersionConflictError) as conflict:
        await files.read("changed.bin", version="v1")
    assert isinstance(conflict.value, ConflictError)
    assert conflict.value.status == 409
    assert conflict.value.code == "drive_file_version_conflict"
    assert conflict.value.request_id == "req-drive_file_version_conflict"
    for path, code in [("gone.bin", "drive_file_not_found"), ("deleted-session.bin", "session_not_found")]:
        with pytest.raises(TiDBLinkApiError) as missing:
            await files.read(path, version="v1")
        assert not isinstance(missing.value, ConflictError)
        assert (missing.value.status, missing.value.code) == (404, code)
    # Only the version conflict code is a version conflict.
    with pytest.raises(ConflictError) as other:
        await files.read("stale.json")
    assert not isinstance(other.value, DriveFileVersionConflictError)


async def test_a_large_files_read_follows_the_download_ticket_the_service_answers_with_without_the_api_key() -> None:
    redemptions: list[tuple[str, str | None]] = []

    def ticket(name: str, byte_size: int = 5, expires_at: str = "2099-01-01T00:00:00.000Z") -> dict[str, Any]:
        return {"url": f"/api/v1/drive-downloads/v1.session-file.k1.{name}", "expiresAt": expires_at, "byteSize": byte_size}

    def handler(request: httpx.Request, _: Service) -> httpx.Response:
        if request.url.path == f"{SESSION_PATH}/files/content":
            name = request.url.params["path"].replace(".bin", "")
            if name == "expired":
                answer: Any = ticket(name, 5, "2000-01-01T00:00:00.000Z")
            elif name == "truncated":
                answer = ticket(name, 6)
            elif name == "unusable":
                answer = {"url": ticket(name)["url"], "byteSize": 5}
            else:
                answer = ticket(f"{name}.{request.url.params.get('version', 'current')}")
            return httpx.Response(200, headers={"content-type": TICKET_TYPE}, content=json.dumps(answer).encode())
        redemptions.append((request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith(".changed.v1"):
            return refuse(409, "drive_file_version_conflict")
        if request.url.path.endswith(".gone.v1"):
            return refuse(404, "drive_file_not_found")
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"large")

    files, _ = files_of(handler)
    assert await files.read("big.bin", version="v1") == b"large"
    assert redemptions == [("/api/v1/drive-downloads/v1.session-file.k1.big.v1", None)]
    with pytest.raises(DriveFileVersionConflictError) as conflict:
        await files.read("changed.bin", version="v1")
    assert conflict.value.request_id == "req-drive_file_version_conflict"
    with pytest.raises(TiDBLinkApiError) as missing:
        await files.read("gone.bin", version="v1")
    assert not isinstance(missing.value, ConflictError)
    assert (missing.value.status, missing.value.code) == (404, "drive_file_not_found")
    with pytest.raises(TiDBLinkError, match="size"):
        await files.read("truncated.bin")
    with pytest.raises(TiDBLinkError, match="ticket"):
        await files.read("unusable.bin")
    before = len(redemptions)
    with pytest.raises(TiDBLinkApiError) as expired:
        await files.read("expired.bin")
    assert expired.value.status == 410
    assert len(redemptions) == before


async def test_create_download_ticket_returns_an_absolute_keyless_url_for_the_file_pinned_to_its_version() -> None:
    def handler(request: httpx.Request, _: Service) -> httpx.Response:
        path = json.loads(request.content)["path"]
        if path == "changed.bin":
            return refuse(409, "drive_file_version_conflict")
        if path == "gone.bin":
            return refuse(404, "drive_file_not_found")
        return json_response(
            {
                "url": "/api/v1/drive-downloads/v1.session-file.k1.opaque",
                "expiresAt": "2099-01-01T00:01:00.000Z",
                "byteSize": 4096,
            },
            201,
        )

    files, service = files_of(handler)
    assert await files.create_download_ticket("build/firmware.bin", version="v1") == {
        "url": f"{BASE_URL}/api/v1/drive-downloads/v1.session-file.k1.opaque",
        "expiresAt": "2099-01-01T00:01:00.000Z",
        "byteSize": 4096,
    }
    await files.create_download_ticket("notes.md")
    assert [(call.method, call.path, call.headers["authorization"], call.json()) for call in service.calls] == [
        (
            "POST",
            f"{SESSION_PATH}/files/download-tickets",
            f"Bearer {ORGANIZATION_API_KEY}",
            {"path": "build/firmware.bin", "version": "v1"},
        ),
        ("POST", f"{SESSION_PATH}/files/download-tickets", f"Bearer {ORGANIZATION_API_KEY}", {"path": "notes.md"}),
    ]
    with pytest.raises(DriveFileVersionConflictError):
        await files.create_download_ticket("changed.bin", version="v1")
    with pytest.raises(TiDBLinkApiError) as missing:
        await files.create_download_ticket("gone.bin", version="v1")
    assert not isinstance(missing.value, ConflictError)
    assert missing.value.code == "drive_file_not_found"


async def test_invalid_session_file_arguments_never_dispatch() -> None:
    files, service = files_of(lambda request, _: httpx.Response(500))
    cases: list[tuple[Any, dict[str, Any], type[Exception]]] = [
        ("", {}, ValueError),
        (7, {}, TypeError),
        ("a.bin", {"version": ""}, ValueError),
        ("a.bin", {"version": "has space"}, ValueError),
        ("a.bin", {"version": "x" * 33}, ValueError),
        ("a.bin", {"version": 1}, TypeError),
        ("a.bin", {"unknown": True}, TypeError),
    ]
    for path, options, error in cases:
        with pytest.raises(error):
            await files.read(path, **options)
        with pytest.raises(error):
            await files.create_download_ticket(path, **options)
    assert service.calls == []
