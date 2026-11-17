import hashlib
import json
from typing import Any

import httpx
import pytest

from files_support import hex_to_base64, read_request, ready
from support import ORGANIZATION_API_KEY
from tidblink_server import OrganizationClient

BASE_URL = "https://tidb-link.test"
STORAGE = "https://storage.test"


def transport(calls: list[Any], file: dict[str, Any]) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        sent = await read_request(request)
        calls.append(sent)
        url = request.url
        if f"{url.scheme}://{url.host}" == STORAGE:
            assert "authorization" not in request.headers
            assert "x-request-id" not in request.headers
            if request.method == "PUT":
                return httpx.Response(200, headers={"etag": '"part-1"'})
            return httpx.Response(200, content=b"abc")
        assert request.headers["authorization"] == f"Bearer {ORGANIZATION_API_KEY}"
        path = url.path
        if path == "/api/v1/org/users":
            return httpx.Response(200, json={"users": []})
        if path.endswith("/drive/uploads"):
            upload = {
                "uploadId": "upload_1",
                "mode": "s3",
                "partSize": 3,
                "totalParts": 1,
                "expectedChecksumVerification": "whole",
                "expiresAt": "2099-01-01T00:00:00.000Z",
                "file": {**file, "status": "uploading"},
            }
            return httpx.Response(200, json={"upload": upload})
        if path.endswith("/presign"):
            part = {
                "number": 1,
                "size": 3,
                "url": f"{STORAGE}/part",
                "headers": {"x-amz-checksum-sha256": hex_to_base64(json.loads(sent.body)["sha256"])},
                "expiresAt": "2099-01-01T00:00:00.000Z",
            }
            return httpx.Response(200, json={"part": part})
        if path.endswith("/complete"):
            return httpx.Response(200, json={"file": file})
        raise AssertionError(f"Unexpected request {request.method} {url}")

    return httpx.MockTransport(handler)


async def test_an_injected_transport_carries_every_request_including_file_transfers() -> None:
    calls: list[Any] = []
    file = {**ready("upload_1"), "createdAt": "2026-11-11T00:00:00.000Z", "updatedAt": "2026-11-11T00:00:00.000Z"}
    async with httpx.AsyncClient(transport=transport(calls, file)) as http_client:
        organization = OrganizationClient(base_url=BASE_URL, api_key=ORGANIZATION_API_KEY, http_client=http_client)
        assert await organization.list_end_users() == []

        user = organization.user("user_1")
        uploaded = await user.files.upload(original_name="note.txt", data=b"abc", idempotency_key="upload-key")
        assert uploaded["status"] == "ready"

        downloaded = await user.files.download_from_grant(
            {
                "url": f"{STORAGE}/download",
                "expiresAt": "2099-01-01T00:00:00.000Z",
                "byteSize": 3,
                "sha256": hashlib.sha256(b"abc").hexdigest(),
            }
        )
        assert downloaded == b"abc"
        await organization.close()

    assert [f"{c.method} {c.url}" for c in calls] == [
        f"GET {BASE_URL}/api/v1/org/users",
        f"POST {BASE_URL}/api/v1/org/users/user_1/drive/uploads",
        f"POST {BASE_URL}/api/v1/org/users/user_1/drive/uploads/upload_1/parts/1/presign",
        f"PUT {STORAGE}/part",
        f"POST {BASE_URL}/api/v1/org/users/user_1/drive/uploads/upload_1/complete",
        f"GET {STORAGE}/download",
    ]
    assert calls[3].body == b"abc"


async def test_file_transfers_carry_only_the_grant_headers_not_the_transports_defaults() -> None:
    calls: list[Any] = []
    file = ready("upload_1")
    async with httpx.AsyncClient(
        transport=transport(calls, file), headers={"x-default": "1"}, cookies={"session": "secret"}
    ) as http_client:
        organization = OrganizationClient(base_url=BASE_URL, api_key=ORGANIZATION_API_KEY, http_client=http_client)
        user = organization.user("user_1")
        await user.files.upload(original_name="note.txt", data=b"abc", idempotency_key="upload-key")
        await user.files.download_from_grant(
            {"url": f"{STORAGE}/download", "expiresAt": "2099-01-01T00:00:00.000Z", "byteSize": 3, "sha256": file["sha256"]}
        )
    storage = [c for c in calls if c.url.startswith(STORAGE)]
    assert len(storage) == 2
    for call in storage:
        assert "x-default" not in call.headers
        assert "cookie" not in call.headers
        assert "authorization" not in call.headers
    assert set(storage[0].headers.keys()) == {"host", "x-amz-checksum-sha256", "content-length"}
    assert set(storage[1].headers.keys()) == {"host"}


def test_http_client_must_be_an_httpx_async_client() -> None:
    with pytest.raises(TypeError):
        OrganizationClient(base_url=BASE_URL, api_key=ORGANIZATION_API_KEY, http_client="fetch")  # type: ignore[arg-type]
