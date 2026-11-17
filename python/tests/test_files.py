import asyncio
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import tidblink_server.files as files_module
from files_support import (
    ABC_BASE64,
    ABC_SHA256,
    EMPTY_SHA256,
    SHAPES,
    STORAGE,
    UploadService,
    hex_to_base64,
    organization,
    read_request,
    ready,
)
from support import ORGANIZATION_API_KEY, error_response, json_response, serve
from tidblink_server import OutcomeUnknownError, TiDBLinkApiError, TiDBLinkConnectionError, TiDBLinkError
from tidblink_server._http import is_unsent_failure
from tidblink_server.files import (
    FileBytes,
    FileDownload,
    FileUploadError,
    FileUploadOutcomeUnknownError,
)

FUTURE = "2099-01-01T00:00:00.000Z"
PAST = "2000-01-01T00:00:00.000Z"


def abc_part(url: str, expires_at: str = FUTURE) -> dict[str, Any]:
    return {"number": 1, "size": 3, "url": url, "headers": {"x-amz-checksum-sha256": ABC_BASE64}, "expiresAt": expires_at}


async def test_file_content_uses_the_owned_binary_endpoint_and_preserves_service_refusal() -> None:
    def handler(request: httpx.Request, service: Any) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {ORGANIZATION_API_KEY}"
        if request.url.path == "/api/v1/org/users/user_1/files/owned/content":
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"owned bytes")
        return httpx.Response(
            404,
            headers={"x-request-id": "file-refusal"},
            json={"error": {"code": "drive_file_not_found", "message": "Not found"}},
        )

    org, _ = serve(handler)
    user = org.user("user_1")
    content = await user.files.get_content("owned")
    assert content == FileBytes(b"owned bytes", "text/plain")
    assert content.type == "bytes"
    with pytest.raises(TiDBLinkApiError) as refused:
        await user.files.get_content("foreign")
    assert refused.value.status == 404
    assert refused.value.request_id == "file-refusal"


async def test_inline_upload_sends_exact_bytes_with_a_complete_digest_confirms_ready_and_recovers_the_same_identity() -> None:
    file = ready("file-inline")

    def handler(request: httpx.Request, service: Any) -> httpx.Response:
        call = service.calls[-1]
        path = request.url.path
        if path.endswith("/uploads"):
            assert call.json() == {
                "originalName": "note.txt",
                "byteSize": 3,
                "sha256": file["sha256"],
                "contentType": "text/plain",
            }
            assert request.headers["idempotency-key"] == "stable-file"
            upload = {
                "uploadId": file["fileId"],
                "mode": "tidb-link",
                "expiresAt": FUTURE,
                "expectedChecksumVerification": "whole",
                "file": {**file, "status": "uploading"},
            }
            return json_response({"upload": upload}, 201)
        if path.endswith("/content"):
            assert call.body == b"abc"
            assert request.headers["content-length"] == "3"
            assert request.headers["content-digest"] == f"sha-256=:{ABC_BASE64}:"
            return json_response({"file": file})
        if path.endswith("/complete"):
            assert call.json() == {}
            return json_response({"file": file})
        return json_response(
            {"upload": {"uploadId": file["fileId"], "mode": "tidb-link", "state": "completed", "expiresAt": FUTURE, "file": file}}
        )

    org, service = serve(handler)
    user = org.user("user_1")
    uploaded = await user.files.upload(
        original_name="note.txt", data=b"abc", content_type="text/plain", idempotency_key="stable-file"
    )
    assert uploaded == file
    assert (await user.files.get_upload(file["fileId"]))["state"] == "completed"
    assert len([c for c in service.calls if c.path.endswith("/uploads")]) == 1


async def test_multipart_upload_follows_the_service_plan_binds_each_checksum_and_never_sends_the_api_key_to_storage() -> None:
    stored: list[bytes] = []
    parts: list[dict[str, Any]] = []
    storage = "http://storage.test"

    def handler(request: httpx.Request, service: Any) -> httpx.Response:
        call = service.calls[-1]
        if request.url.host == "storage.test":
            assert "authorization" not in request.headers
            assert "x-request-id" not in request.headers
            assert request.headers["x-amz-checksum-sha256"]
            stored.append(call.body)
            return httpx.Response(200, headers={"etag": f'"part-{len(stored)}"'})
        body = call.json() if call.body else {}
        path = request.url.path
        if path.endswith("/uploads"):
            return json_response(
                {
                    "upload": {
                        "uploadId": "multipart",
                        "mode": "s3",
                        "partSize": 2,
                        "totalParts": 2,
                        "expectedChecksumVerification": "whole",
                        "expiresAt": FUTURE,
                        "file": {**ready("multipart"), "status": "uploading"},
                    }
                }
            )
        if path.endswith("/presign"):
            number = int(path.split("/")[-2])
            parts.append({"number": number, "sha256": body["sha256"]})
            return json_response(
                {
                    "part": {
                        "number": number,
                        "size": 2 if number == 1 else 1,
                        "url": f"{storage}/part",
                        "headers": {"x-amz-checksum-sha256": hex_to_base64(body["sha256"])},
                        "expiresAt": FUTURE,
                    }
                }
            )
        assert body["parts"] == [{**p, "etag": f'"part-{p["number"]}"'} for p in parts]
        return json_response({"file": ready("multipart")})

    org, _ = serve(handler)
    uploaded = await org.user("user_1").files.upload(original_name="note.txt", data=b"abc", idempotency_key="multipart-key")
    assert uploaded["status"] == "ready"
    assert b"".join(stored) == b"abc"
    assert parts[0]["sha256"] == "fb8e20fc2e4c3f248c60c39bd652f3c1347298bb977b8b4d5903b85055620603"
    assert parts[1]["sha256"] == "2e7d2c03a9507ae265ecf5b5356885a53393a2029d241394997265a1a25aefc6"


async def test_empty_inline_uploads_use_the_supplied_short_lived_url_without_an_api_key() -> None:
    file = ready("empty", b"")
    assert file["sha256"] == EMPTY_SHA256
    digest = "sha-256=:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=:"

    def handler(request: httpx.Request, service: Any) -> httpx.Response:
        if request.url.host == "storage.test":
            assert "authorization" not in request.headers
            assert request.headers["content-length"] == "0"
            assert service.calls[-1].body == b""
            assert request.headers["content-digest"] == digest
            assert request.url.query == b"secret=ticket"
            return json_response({"file": file})
        if request.url.path.endswith("/uploads"):
            return json_response(
                {
                    "upload": {
                        "uploadId": file["fileId"],
                        "mode": "tidb-link",
                        "expiresAt": FUTURE,
                        "expectedChecksumVerification": "whole",
                        "file": {**file, "status": "uploading"},
                        "uploadUrl": {
                            "method": "PUT",
                            "url": "http://storage.test/upload?secret=ticket",
                            "headers": {"content-type": "application/octet-stream", "content-digest": digest},
                            "expiresAt": FUTURE,
                        },
                    }
                }
            )
        return json_response({"file": file})

    org, service = serve(handler)
    assert await org.user("user_1").files.upload(original_name="empty", data=b"", idempotency_key="empty-key") == file
    assert [c.url.host for c in service.calls].count("storage.test") == 1


async def test_file_listing_preserves_its_own_cursor_history_semantics_and_verified_downloads_use_ticket_authority_only() -> None:
    file = ready("download")
    grant = {"url": "http://storage.test/download?ticket=secret", "expiresAt": FUTURE, "byteSize": 3, "sha256": file["sha256"]}

    def handler(request: httpx.Request, service: Any) -> httpx.Response:
        if request.url.host == "storage.test":
            assert "authorization" not in request.headers
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"abc")
        path = request.url.path
        if path == "/api/v1/org/users/user_1/files":
            query = parse_qs(urlsplit(str(request.url)).query)
            assert query == {"history": ["true"], "limit": ["1"], "cursor": ["cursor/+"]}
            return json_response({"files": [file], "nextCursor": "next"})
        if path.endswith("/content"):
            return httpx.Response(
                200, headers={"content-type": "application/vnd.tidb-link.file-download+json"}, json=grant
            )
        return json_response({"file": file})

    org, _ = serve(handler)
    user = org.user("user_1")
    assert await user.files.list(history=True, limit=1, cursor="cursor/+") == {"files": [file], "nextCursor": "next"}
    content = await user.files.get_content("download")
    assert content == FileDownload(grant)  # type: ignore[arg-type]
    assert content.type == "download"
    assert await user.files.download("download") == b"abc"


async def test_uncertain_file_creation_keeps_its_key_and_never_repeats_the_effect() -> None:
    # Python strings are immutable, so the TS check that a caller's later mutation
    # of the input object cannot change the reported key has no counterpart.
    def handler(request: httpx.Request, service: Any) -> httpx.Response:
        raise httpx.ReadError("connection dropped")

    org, service = serve(handler)
    with pytest.raises(FileUploadOutcomeUnknownError) as caught:
        await org.user("user_1").files.upload(original_name="x", data=b"", idempotency_key="recover-original")
    assert caught.value.idempotency_key == "recover-original"
    assert caught.value.upload_id is None
    assert isinstance(caught.value, OutcomeUnknownError)
    assert [c.headers["idempotency-key"] for c in service.calls] == ["recover-original"]


async def test_uncertain_multipart_transfers_retain_upload_identity_and_do_not_complete_or_replay_effects() -> None:
    state = {"transfers": 0, "complete": False}

    def handler(request: httpx.Request, service: Any) -> httpx.Response:
        if request.url.host == "storage.test":
            state["transfers"] += 1
            return httpx.Response(503)
        path = request.url.path
        if path.endswith("/uploads"):
            return json_response(
                {
                    "upload": {
                        "uploadId": "uncertain-file",
                        "mode": "s3",
                        "partSize": 3,
                        "totalParts": 1,
                        "expiresAt": FUTURE,
                        "expectedChecksumVerification": "whole",
                        "file": {**ready("uncertain-file"), "status": "uploading"},
                    }
                }
            )
        if path.endswith("/presign"):
            sha256 = service.calls[-1].json()["sha256"]
            return json_response(
                {
                    "part": {
                        "number": 1,
                        "size": 3,
                        "url": "http://storage.test",
                        "headers": {"x-amz-checksum-sha256": hex_to_base64(sha256)},
                        "expiresAt": FUTURE,
                    }
                }
            )
        state["complete"] = True
        return json_response({"file": ready("uncertain-file")})

    org, _ = serve(handler)
    with pytest.raises(FileUploadOutcomeUnknownError) as caught:
        await org.user("user_1").files.upload(original_name="x", data=b"abc", idempotency_key="retain-me")
    assert caught.value.upload_id == "uncertain-file"
    assert caught.value.idempotency_key == "retain-me"
    assert state == {"transfers": 1, "complete": False}


async def test_expired_and_mismatched_part_grants_are_refused_before_any_bytes_are_sent() -> None:
    org, service = serve(lambda request, service: httpx.Response(200))
    files = org.user("user_1").files
    part = abc_part("http://storage.test/part", PAST)
    with pytest.raises(TiDBLinkApiError) as expired:
        await files.upload_part(part, b"abc")  # type: ignore[arg-type]
    assert expired.value.code == "file_transfer_expired"
    with pytest.raises(ValueError, match="checksum"):
        await files.upload_part({**part, "expiresAt": FUTURE}, b"bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="size"):
        await files.upload_part(part, b"abcd")  # type: ignore[arg-type]
    assert service.calls == []


async def test_upload_and_authenticated_content_redirects_never_send_keys_to_another_destination() -> None:
    def handler(request: httpx.Request, service: Any) -> httpx.Response:
        assert request.url.host != "other.test", "a redirect was followed"
        return httpx.Response(307, headers={"location": "http://other.test/redirected?ticket=hidden"})

    org, service = serve(handler)
    files = org.user("user_1").files
    with pytest.raises(TiDBLinkError):
        await files.get_content("file")
    with pytest.raises(TiDBLinkError) as caught:
        await files.upload_part(abc_part("http://tidblink.test/signed?secret=not-for-errors"), b"abc")  # type: ignore[arg-type]
    assert "secret=" not in str(caught.value)
    assert ORGANIZATION_API_KEY not in str(caught.value)
    assert "authorization" not in service.calls[-1].headers
    assert all(c.url.host == "tidblink.test" for c in service.calls)


async def test_download_checks_complete_content_and_keeps_expired_grants_unusable() -> None:
    org, service = serve(lambda request, service: httpx.Response(200, content=b"abd"))
    files = org.user("user_1").files
    grant = {"url": "http://storage.test", "expiresAt": FUTURE, "byteSize": 3, "sha256": ABC_SHA256}
    with pytest.raises(TiDBLinkError, match="checksum"):
        await files.download_from_grant(grant)  # type: ignore[arg-type]
    with pytest.raises(TiDBLinkApiError) as expired:
        await files.download_from_grant({**grant, "expiresAt": PAST})  # type: ignore[arg-type]
    assert expired.value.code == "file_transfer_expired"
    assert len(service.calls) == 1


async def test_a_download_follows_one_https_redirect_without_an_api_key() -> None:
    grant = {"url": "http://storage.test/download?ticket=t", "expiresAt": FUTURE, "byteSize": 3, "sha256": ABC_SHA256}
    target = {"location": "https://cdn.test/object?signature=s"}

    def handler(request: httpx.Request, service: Any) -> httpx.Response:
        assert "authorization" not in request.headers
        if request.url.host == "storage.test":
            return httpx.Response(302, headers=target)
        if request.url.path == "/again":
            return httpx.Response(302, headers={"location": "https://cdn.test/object"})
        return httpx.Response(200, content=b"abc")

    org, service = serve(handler)
    files = org.user("user_1").files
    assert await files.download_from_grant(grant) == b"abc"  # type: ignore[arg-type]
    assert [str(c.url) for c in service.calls] == [grant["url"], target["location"]]

    target["location"] = "http://cdn.test/object"
    with pytest.raises(TiDBLinkError, match="HTTPS"):
        await files.download_from_grant(grant)  # type: ignore[arg-type]
    target.pop("location")
    with pytest.raises(TiDBLinkError, match="omitted"):
        await files.download_from_grant(grant)  # type: ignore[arg-type]
    target["location"] = "https://cdn.test/again"
    with pytest.raises(TiDBLinkConnectionError, match="could not be reached"):
        await files.download_from_grant(grant)  # type: ignore[arg-type]
    assert str(service.calls[-1].url) == "https://cdn.test/again"


async def test_upload_sends_the_callers_own_bytes_in_place_once_with_an_explicit_length() -> None:
    # TS distinguishes Node's fetch (a stream of the caller's array) from an
    # injected fetch (the array itself). Python always sends through httpx with
    # a body that hands over views of the caller's buffer, so one test covers both.
    for shape in SHAPES:
        service = UploadService(shape)
        data = bytearray(b"abc")
        async with organization(service) as org:
            file = await org.user("user_1").files.upload(original_name="note.txt", data=data, idempotency_key=f"stream-{shape}")
        assert file["status"] == "ready"
        requests = service.byte_requests(data)
        assert len(requests) == (2 if shape == "multipart" else 1), shape
        for sent, expected in requests:
            assert sent.method == "PUT", shape
            assert sent.headers["content-length"] == str(len(expected)), shape
            assert "transfer-encoding" not in sent.headers, shape
            assert sent.chunks is not None, shape
            # One chunk: a view of the caller's buffer, not a copy.
            assert len(sent.chunks) == 1, shape
            assert isinstance(sent.chunks[0], memoryview) and sent.chunks[0].obj is data, shape
            assert sent.body == expected, shape
            if shape != "content":
                assert "authorization" not in sent.headers, shape
        assert data == b"abc"


async def test_the_explicit_steps_send_the_callers_bytes_in_place_and_refuse_other_types() -> None:
    service = UploadService("content")
    async with organization(service) as org:
        files = org.user("user_1").files
        content = b"abc"
        await files.upload_content("shaped", content)
        part = memoryview(bytearray(b"ab"))
        uploaded = await files.upload_part(
            {
                "number": 1,
                "size": 2,
                "url": f"{STORAGE}/part",
                "headers": {
                    "x-amz-checksum-sha256": hex_to_base64(
                        "fb8e20fc2e4c3f248c60c39bd652f3c1347298bb977b8b4d5903b85055620603"
                    )
                },
                "expiresAt": FUTURE,
            },
            part,
        )
        assert uploaded == {
            "number": 1,
            "etag": '"p"',
            "sha256": "fb8e20fc2e4c3f248c60c39bd652f3c1347298bb977b8b4d5903b85055620603",
        }
        assert service.sent[0].chunks is not None and service.sent[0].chunks[0].obj is content
        assert service.sent[0].headers["content-digest"] == f"sha-256=:{ABC_BASE64}:"
        assert service.sent[1].chunks is not None and service.sent[1].chunks[0].obj is part.obj
        with pytest.raises(TypeError, match="bytes"):
            await files.upload_content("shaped", "abc")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="bytes"):
            await files.upload_part({}, [1, 2])  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="FileUploadPart"):
            await files.upload_part({}, b"ab")  # type: ignore[arg-type]
    assert len(service.sent) == 2


async def test_a_byte_transfer_that_sent_nothing_fails_as_itself_not_as_an_unknown_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    # The transport fails before it reads the body. In TS only Node's own fetch
    # shows this; in Python every http_client sends a body the SDK can observe.
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with organization(refused) as org:
        files = org.user("user_1").files
        with pytest.raises(TiDBLinkConnectionError) as content_error:
            await files.upload_content("inline", b"abc")
        assert not isinstance(content_error.value, OutcomeUnknownError)
        assert is_unsent_failure(content_error.value)
        with pytest.raises(TiDBLinkConnectionError) as part_error:
            await files.upload_part(abc_part(f"{STORAGE}/part"), b"abc")  # type: ignore[arg-type]
        assert not isinstance(part_error.value, OutcomeUnknownError)
        assert is_unsent_failure(part_error.value)
        # Grant headers the transport would refuse are a local error.
        bad = abc_part(f"{STORAGE}/part")
        bad["headers"] = {**bad["headers"], "x-bad": "a\nb"}
        with pytest.raises(ValueError, match="header"):
            await files.upload_part(bad, b"abc")  # type: ignore[arg-type]

    # Bytes whose length no longer matches what was hashed stop the body locally.
    calls: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return json_response({"file": ready("inline")})

    real_digest = files_module._digest
    monkeypatch.setattr(
        files_module,
        "_digest",
        lambda view: files_module._Checksum(real_digest(view).hex, real_digest(view).base64, view.nbytes + 1),
    )
    async with organization(recording) as org:
        with pytest.raises(ValueError, match="changed size") as changed:
            await org.user("user_1").files.upload_content("inline", b"abc")
        assert is_unsent_failure(changed.value)
    monkeypatch.setattr(files_module, "_digest", real_digest)
    assert calls == []

    # Once the service may have received the body, the outcome is unknown.
    async def dropping(request: httpx.Request) -> httpx.Response:
        await request.aread()
        raise httpx.ReadError("connection dropped")

    async with organization(dropping) as org:
        files = org.user("user_1").files
        with pytest.raises(OutcomeUnknownError):
            await files.upload_content("inline", b"abc")
        with pytest.raises(OutcomeUnknownError):
            await files.upload_part(abc_part(f"{STORAGE}/part"), b"abc")  # type: ignore[arg-type]


async def test_upload_transfers_that_sent_nothing_carry_the_upload_and_key_apart_from_unknown_outcomes() -> None:
    async def unsent(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def read_then_fail(request: httpx.Request) -> httpx.Response:
        await request.aread()
        raise httpx.ReadError("connection dropped")

    for transfer, expected in ((unsent, FileUploadError), (read_then_fail, FileUploadOutcomeUnknownError)):
        async with organization(UploadService("content", transfer=transfer)) as org:
            with pytest.raises(TiDBLinkError) as caught:
                await org.user("user_1").files.upload(original_name="note.txt", data=b"abc", idempotency_key="known-key")
        error = caught.value
        assert type(error) is expected
        assert error.upload_id == "shaped"  # type: ignore[attr-defined]
        assert error.idempotency_key == "known-key"  # type: ignore[attr-defined]
        if expected is FileUploadError:
            assert isinstance(error.cause, TiDBLinkConnectionError)

    # Cancellation propagates unchanged (TS wraps an aborted, unsent transfer in FileUploadError).
    started = asyncio.Event()

    async def hanging(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    service = UploadService("content", transfer=hanging)
    async with organization(service) as org:
        task = asyncio.create_task(
            org.user("user_1").files.upload(original_name="note.txt", data=b"abc", idempotency_key="cancelled")
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [s.url.rsplit("/", 1)[-1] for s in service.sent] == ["uploads"]

    # A refusal of the transfer is the service's error, as before.
    async def refusing(request: httpx.Request) -> httpx.Response:
        await request.aread()
        return error_response(422, "drive_upload_refused", "No")

    async with organization(UploadService("content", transfer=refusing)) as org:
        with pytest.raises(TiDBLinkApiError) as refused:
            await org.user("user_1").files.upload(original_name="note.txt", data=b"abc", idempotency_key="refused")
    assert type(refused.value) is TiDBLinkApiError
    assert refused.value.code == "drive_upload_refused"

    # A failure before the upload exists is an unknown outcome without an upload.
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with organization(failing) as org:
        with pytest.raises(FileUploadOutcomeUnknownError) as unknown:
            await org.user("user_1").files.upload(original_name="note.txt", data=b"abc", idempotency_key="no-upload")
    assert unknown.value.upload_id is None


async def test_bytes_that_change_size_after_hashing_are_never_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Python cannot resize a bytearray while the SDK holds a view of it, so a
    # size change is simulated by a digest that reports another length.
    real_digest = files_module._digest
    monkeypatch.setattr(
        files_module,
        "_digest",
        lambda view: files_module._Checksum(real_digest(view).hex, real_digest(view).base64, view.nbytes + 1),
    )
    for shape in SHAPES:
        service = UploadService(shape)
        data = b"abc"
        async with organization(service) as org:
            with pytest.raises(FileUploadError) as caught:
                await org.user("user_1").files.upload(original_name="note.txt", data=data, idempotency_key=f"changed-{shape}")
        assert isinstance(caught.value.cause, ValueError)
        assert "changed size" in str(caught.value.cause)
        assert service.byte_requests(data) == [], shape
    monkeypatch.setattr(files_module, "_digest", real_digest)

    # The caller cannot resize its bytearray while the upload holds it.
    data = bytearray(b"abc")
    resized: list[BaseException] = []

    def resize() -> None:
        try:
            data.extend(b"d")
        except BufferError as error:
            resized.append(error)

    async with organization(UploadService("content", on_create=resize)) as org:
        assert (await org.user("user_1").files.upload(original_name="note.txt", data=data, idempotency_key="locked"))["status"] == "ready"
    assert len(resized) == 1 and data == b"abc"


async def test_upload_validates_its_input_before_any_request() -> None:
    service = UploadService("content")
    async with organization(service) as org:
        files = org.user("user_1").files
        with pytest.raises(ValueError, match="idempotency_key"):
            await files.upload(original_name="note.txt", data=b"abc", idempotency_key="has space")
        with pytest.raises(ValueError, match="name"):
            await files.upload(original_name="", data=b"abc", idempotency_key="k")
        with pytest.raises(TypeError, match="bytes"):
            await files.upload(original_name="note.txt", data="abc", idempotency_key="k")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="positive number"):
            await files.presign_part("shaped", 0, ABC_SHA256)
        with pytest.raises(TypeError, match="history"):
            await files.list(history="true")  # type: ignore[arg-type]
    assert service.sent == []


async def test_upload_file_example_uploads_then_attaches_the_file_to_a_turn() -> None:
    from examples.upload_file import upload_file_workflow
    from support import interaction

    service = UploadService("content")
    turns: list[Any] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/events"):
            sent = await read_request(request)
            turns.append(json.loads(sent.body))
            return json_response({"interaction": interaction(inputId="event-1")}, 202)
        if "/interactions/" in path:
            return json_response(
                {
                    "interaction": interaction(
                        inputId="event-1",
                        appliedAt="2026-10-28T00:00:01Z",
                        result={"status": "succeeded", "completedAt": "2026-10-28T00:00:02Z"},
                    )
                }
            )
        if "/turns/" in path:
            return json_response(
                {
                    "turn": {
                        "id": "turn_1",
                        "sessionId": "session_1",
                        "status": "succeeded",
                        "completedAt": "2026-10-28T00:00:02Z",
                        "assistantMessage": {"id": "m2", "role": "assistant", "content": "Read it", "turnId": "turn_1", "sessionId": "session_1"},
                    }
                }
            )
        return await service(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        file, result = await upload_file_workflow(
            base_url="https://core.example",
            organization_api_key=ORGANIZATION_API_KEY,
            user_id="user_1",
            session_id="session_1",
            original_name="note.txt",
            data=b"abc",
            idempotency_key="upload-key",
            client_event_id="event-1",
            http_client=http_client,
        )
    assert file["fileId"] == "shaped"
    assert result.text == "Read it"
    assert turns[0]["data"]["userFileIds"] == ["shaped"]
    assert turns[0]["clientEventId"] == "event-1"
