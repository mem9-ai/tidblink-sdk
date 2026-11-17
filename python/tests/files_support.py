"""Helpers for file tests: File records and a fake service and storage for the three upload shapes."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from support import ORGANIZATION_API_KEY, json_response
from tidblink_server import OrganizationClient

CORE = "https://core.example"
STORAGE = "https://storage.example"
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
ABC_BASE64 = "ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0="
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SHAPES = ("content", "grant", "multipart")


def ready(file_id: str, content: bytes = b"abc") -> dict[str, Any]:
    return {
        "fileId": file_id,
        "originalName": "note.txt",
        "status": "ready",
        "byteSize": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "checksumVerification": "whole",
        "contentType": "text/plain",
        "lastError": None,
        "source": {"kind": "user_upload"},
        "createdAt": "2026-10-28T00:00:00.000Z",
        "updatedAt": "2026-10-28T00:00:00.000Z",
    }


def hex_to_base64(value: str) -> str:
    return base64.b64encode(bytes.fromhex(value)).decode()


@dataclass
class Sent:
    method: str
    url: str
    headers: httpx.Headers
    body: bytes
    #: The chunks the transport pulled from a streamed body, or None for a buffered one.
    chunks: list[Any] | None


async def read_request(request: httpx.Request) -> Sent:
    """Read a request like a server would, keeping the chunks of a streamed body as they were handed over."""
    chunks: list[Any] | None = None
    if isinstance(request.stream, httpx.ByteStream):
        body = await request.aread()
    else:
        chunks = [chunk async for chunk in request.stream]  # type: ignore[union-attr]
        body = b"".join(chunks)
    return Sent(request.method, str(request.url), request.headers, body, chunks)


Transfer = Callable[[httpx.Request], Awaitable[httpx.Response]]


class UploadService:
    """A fake service and storage for one upload shape: inline content, an inline grant, or multipart."""

    def __init__(
        self,
        shape: str,
        *,
        transfer: Transfer | None = None,
        on_create: Callable[[], Any] | None = None,
    ) -> None:
        self.shape = shape
        self.transfer = transfer
        self.on_create = on_create
        self.sent: list[Sent] = []

    @staticmethod
    def carries_bytes(url: str) -> bool:
        return url.endswith("/content") or url.startswith(f"{STORAGE}/")

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if self.transfer is not None and self.carries_bytes(url):
            return await self.transfer(request)
        sent = await read_request(request)
        self.sent.append(sent)
        if url.endswith("/uploads"):
            if self.on_create is not None:
                result = self.on_create()
                if inspect.isawaitable(result):
                    await result
            upload: dict[str, Any] = {
                "uploadId": "shaped",
                "mode": "s3" if self.shape == "multipart" else "tidb-link",
                "expiresAt": "2099-01-01T00:00:00.000Z",
                "expectedChecksumVerification": "whole",
                "file": {**ready("shaped"), "status": "uploading"},
            }
            if self.shape == "grant":
                upload["uploadUrl"] = {
                    "method": "PUT",
                    "url": f"{STORAGE}/inline?ticket=t",
                    "headers": {"content-digest": f"sha-256=:{ABC_BASE64}:"},
                    "expiresAt": "2099-01-01T00:00:00.000Z",
                }
            if self.shape == "multipart":
                upload.update(partSize=2, totalParts=2)
            return json_response({"upload": upload})
        if url.endswith("/presign"):
            number = int(url.split("/")[-2])
            sha256 = json.loads(sent.body)["sha256"]
            return json_response(
                {
                    "part": {
                        "number": number,
                        "size": 2 if number == 1 else 1,
                        "url": f"{STORAGE}/part-{number}",
                        "headers": {"x-amz-checksum-sha256": hex_to_base64(sha256)},
                        "expiresAt": "2099-01-01T00:00:00.000Z",
                    }
                }
            )
        if url.startswith(f"{STORAGE}/"):
            return httpx.Response(200, headers={"etag": '"p"'})
        return json_response({"file": ready("shaped")})

    def byte_requests(self, data: bytes | bytearray) -> list[tuple[Sent, bytes]]:
        """The requests that carried file bytes, with the bytes each should carry."""
        requests = [sent for sent in self.sent if self.carries_bytes(sent.url)]
        return [
            (sent, bytes(data[index * 2 : index * 2 + 2]) if self.shape == "multipart" else bytes(data))
            for index, sent in enumerate(requests)
        ]


class LazyTransport(httpx.AsyncBaseTransport):
    """Like ``httpx.MockTransport``, but leaves the request body for ``handler`` to read, or not.

    A real transport pulls the body only as it sends it, so a handler that
    raises before reading it models a request that sent no body bytes.
    """

    def __init__(self, handler: Callable[[httpx.Request], Any]) -> None:
        self.handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        result = self.handler(request)
        if inspect.isawaitable(result):
            result = await result
        return result


def organization(
    handler: Callable[[httpx.Request], Any], base_url: str = CORE, **client_options: Any
) -> OrganizationClient:
    """An OrganizationClient whose every request, including file transfers, goes to ``handler``."""
    return OrganizationClient(
        base_url=base_url,
        api_key=ORGANIZATION_API_KEY,
        http_client=httpx.AsyncClient(transport=LazyTransport(handler), **client_options),
    )
