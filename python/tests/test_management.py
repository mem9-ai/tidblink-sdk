"""Templates and Skills (``management.test.mjs`` in the TypeScript SDK)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from support import Call, Service, json_response, serve
from tidblink_server import ConflictError, OutcomeUnknownError, TiDBLinkError

summary = {
    "id": "tmpl_1",
    "organizationId": "org_1",
    "name": "assistant",
    "displayName": "Assistant",
    "sandboxProfile": "default",
    "lifecycleStatus": "active",
    "etag": '"cvc1.' + "a" * 43 + '"',
    "contentDigest": "b" * 64,
    "createdAt": "2026-11-11T00:00:00.000Z",
    "updatedAt": "2026-11-11T00:00:00.000Z",
}
content = {
    "identity": {"agentName": "Assistant"},
    "instructions": "Be brief.",
    "sandbox": {"profileId": "default"},
    "realtime": {"language": "en-US"},
}
template = {"template": summary, "content": content}


def org_path(call: Call) -> str:
    """The request path and query below the Organization API."""
    return call.path.removeprefix("/api/v1/org")


def described(calls: list[Call]) -> list[str]:
    return [f"{call.method} {org_path(call)}" for call in calls]


async def test_templates_are_created_read_listed_replaced_and_archived_with_their_concurrency_headers() -> None:
    def handler(request: httpx.Request, service: Service) -> httpx.Response:
        path = org_path(service.calls[-1])
        if request.method == "GET" and path.startswith("/agent-templates?"):
            return json_response({"data": [summary], "hasMore": False, "nextCursor": None, "readAt": summary["createdAt"]})
        if path == "/agent-template-reference-catalog":
            return json_response(
                {"models": [], "sandboxes": [{"id": "default", "label": "Default", "capabilities": []}], "skills": []}
            )
        return json_response(template, 201 if request.method == "POST" and path == "/agent-templates" else 200)

    organization, service = serve(handler)
    async with organization:
        templates = organization.templates
        assert await templates.create(display_name="Assistant", content=content, idempotency_key="create-1") == template
        assert await templates.get("tmpl_1") == template
        page = await templates.list(lifecycle_status="active", q="assist", limit=10)
        assert page["data"][0]["id"] == "tmpl_1"
        assert (await templates.catalog())["sandboxes"][0]["id"] == "default"
        await templates.replace(
            "tmpl_1",
            display_name="Assistant",
            content={**content, "instructions": "Be kind."},
            if_match=summary["etag"],
            idempotency_key="replace-1",
        )
        await templates.archive("tmpl_1", if_match=summary["etag"], idempotency_key="archive-1")

    calls = service.calls
    assert described(calls) == [
        "POST /agent-templates",
        "GET /agent-templates/tmpl_1",
        "GET /agent-templates?lifecycleStatus=active&q=assist&limit=10",
        "GET /agent-template-reference-catalog",
        "PUT /agent-templates/tmpl_1",
        "POST /agent-templates/tmpl_1/archive",
    ]
    assert calls[0].headers["idempotency-key"] == "create-1"
    assert calls[0].json() == {"displayName": "Assistant", "content": content}
    assert calls[4].json() == {"displayName": "Assistant", "content": {**content, "instructions": "Be kind."}}
    for index, key in ((4, "replace-1"), (5, "archive-1")):
        assert calls[index].headers["if-match"] == summary["etag"]
        assert calls[index].headers["idempotency-key"] == key
    assert calls[5].json() == {}


async def test_template_creation_is_retried_with_its_key_and_a_stale_etag_is_a_conflict_error() -> None:
    def handler(request: httpx.Request, service: Service) -> httpx.Response:
        if len(service.calls) == 1:
            raise httpx.ConnectError("socket closed")
        if request.method == "PUT":
            return json_response({"error": {"code": "resource_conflict", "message": "Stale"}}, 409)
        return json_response(template, 201)

    organization, service = serve(handler)
    async with organization:
        await organization.templates.create(display_name="Assistant", content=content, idempotency_key="create-1")
        calls = service.calls
        assert len(calls) == 2
        assert calls[1].headers["idempotency-key"] == "create-1"
        assert calls[0].headers["x-request-id"] == calls[1].headers["x-request-id"]
        with pytest.raises(ConflictError):
            await organization.templates.replace(
                "tmpl_1", display_name="Assistant", content=content, if_match='"old"', idempotency_key="replace-1"
            )


async def test_template_inputs_are_checked_before_any_request() -> None:
    organization, service = serve(lambda request, service: json_response(template))
    templates: Any = organization.templates
    async with organization:
        with pytest.raises(TypeError):
            await templates.create(display_name="A", content=content)
        with pytest.raises(ValueError):
            await templates.create(display_name="", content=content, idempotency_key="k")
        with pytest.raises(TypeError):
            await templates.create(display_name="A", content=["not", "a", "mapping"], idempotency_key="k")
        with pytest.raises(ValueError):
            await templates.create(display_name="A", content=content, idempotency_key=" bad key")
        with pytest.raises(TypeError):
            await templates.replace("tmpl_1", display_name="A", content=content, idempotency_key="k")
        with pytest.raises(ValueError):
            await templates.replace("tmpl_1", display_name="A", content=content, if_match="", idempotency_key="k")
        with pytest.raises(TypeError):
            await templates.archive("tmpl_1", if_match='"e"', idempotency_key="k", force=True)
        with pytest.raises(TypeError):
            await templates.list(status="active")
        with pytest.raises(TypeError):
            await templates.list(limit="10")
    assert service.calls == []


async def test_an_unusable_template_receipt_is_an_uncertain_outcome() -> None:
    organization, _ = serve(lambda request, service: json_response({"template": None}, 201))
    async with organization:
        with pytest.raises(OutcomeUnknownError):
            await organization.templates.create(display_name="A", content=content, idempotency_key="k")


def upload(state: str, **extra: Any) -> dict[str, Any]:
    return {
        "upload": {
            "uploadId": "upl_1",
            "skillId": "skill_1",
            "state": state,
            "byteSize": 3,
            "sha256": "c" * 64,
            "expiresAt": "2099-01-01T00:00:00.000Z",
            "validation": {
                "entriesTotal": 1,
                "fileCount": 1,
                "entriesDone": 1,
                "declaredBytes": 3,
                "bytesDone": 3,
                "diagnostics": [],
                "failureCode": None,
                "failurePath": None,
            },
            "version": None,
            "createdAt": "2026-11-11T00:00:00.000Z",
            "updatedAt": "2026-11-11T00:00:00.000Z",
            **extra,
        }
    }


async def test_a_skill_zip_is_uploaded_as_bytes_validated_published_and_read() -> None:
    states = ["pending", "validating", "awaiting_confirmation", "finalizing", "done"]

    def handler(request: httpx.Request, service: Service) -> httpx.Response:
        path = org_path(service.calls[-1])
        if request.method == "POST" and path == "/skills/uploads":
            return json_response(upload("pending"), 202)
        if request.method == "GET" and path == "/skills/uploads/upl_1":
            state = states.pop(0)
            return json_response(upload(state, **({"version": 1} if state == "done" else {})))
        if path == "/skills/uploads/upl_1/publish":
            return json_response(upload("finalizing"), 202)
        if path.startswith("/skills?"):
            return json_response({"skills": [], "nextCursor": None})
        if path == "/skills/skill_1":
            return json_response({"skill": {"skillId": "skill_1"}, "versions": [], "nextCursor": None})
        raise AssertionError(f"unexpected {request.method} {path}")

    organization, service = serve(handler)
    async with organization:
        skills = organization.skills
        data = bytearray([0x50, 0x4B, 0x05])
        created = await skills.upload(data=data, idempotency_key="zip-1")
        assert created["state"] == "pending"
        assert (await skills.wait_for_upload("upl_1", poll_interval=0))["state"] == "awaiting_confirmation"
        await skills.publish("upl_1", idempotency_key="publish-1")
        done = await skills.wait_for_upload("upl_1", until=["done", "failed"], poll_interval=0)
        assert done["version"] == 1
        await skills.list(limit=5)
        await skills.get("skill_1")

    calls = service.calls
    assert calls[0].headers["content-type"] == "application/octet-stream"
    assert calls[0].headers["idempotency-key"] == "zip-1"
    assert calls[0].body == bytes(data)
    assert "GET /skills?limit=5" in described(calls)
    publish = next(call for call in calls if org_path(call).endswith("/publish"))
    assert publish.method == "POST"
    assert publish.headers["idempotency-key"] == "publish-1"
    assert publish.body == b""
    assert "content-type" not in publish.headers


async def test_a_skill_upload_is_retried_with_its_key_and_an_unusable_answer_is_uncertain() -> None:
    def handler(request: httpx.Request, service: Service) -> httpx.Response:
        if len(service.calls) == 1:
            raise httpx.ConnectError("socket closed")
        if len(service.calls) == 2:
            return json_response(upload("pending"), 202)
        return httpx.Response(202, content=b"not json")

    organization, service = serve(handler)
    async with organization:
        await organization.skills.upload(data=memoryview(b"PK\x05"), idempotency_key="zip-1")
        assert [call.body for call in service.calls] == [b"PK\x05", b"PK\x05"]
        assert service.calls[0].headers["x-request-id"] == service.calls[1].headers["x-request-id"]
        with pytest.raises(OutcomeUnknownError):
            await organization.skills.upload(data=b"PK\x05", idempotency_key="zip-2")


async def test_waiting_for_a_skill_upload_stops_at_its_local_limit_and_cancellation_sends_delete() -> None:
    def handler(request: httpx.Request, service: Service) -> httpx.Response:
        return httpx.Response(204) if request.method == "DELETE" else json_response(upload("validating"))

    organization, service = serve(handler)
    async with organization:
        with pytest.raises(TiDBLinkError, match="still validating"):
            await organization.skills.wait_for_upload("upl_1", timeout=0, poll_interval=0.001)
        await organization.skills.cancel_upload("upl_1")
        assert service.calls[-1].method == "DELETE"
        assert org_path(service.calls[-1]) == "/skills/uploads/upl_1"
        count = len(service.calls)
        with pytest.raises(ValueError):
            await organization.skills.upload(data=b"", idempotency_key="k")
        with pytest.raises(TypeError):
            await organization.skills.upload(data="PK", idempotency_key="k")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await organization.skills.wait_for_upload("upl_1", timeout=-1)
        assert len(service.calls) == count
