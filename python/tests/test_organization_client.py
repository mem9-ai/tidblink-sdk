"""Ported from typescript/test/organization-client.test.mjs."""

from __future__ import annotations

import traceback

import httpx
import pytest

from core_support import KEY, disconnect
from support import BASE_URL, error_response, json_response, serve
from tidblink_server import (
    ConflictError,
    OrganizationClient,
    OutcomeUnknownError,
    TiDBLinkApiError,
    TiDBLinkConnectionError,
    TiDBLinkError,
)

IDENTITY_SELECTORS = [
    f"x-{vendor}-{kind}-id"
    for vendor in ("agent9", "ti")
    for kind in ("workspace", "user", "organization", "project")
]


def rendered(error: BaseException) -> list[str]:
    return [str(error), repr(error), "".join(traceback.format_exception(error))]


async def test_retries_a_safe_read_with_one_request_identity() -> None:
    def handler(request, service):
        if len(service.calls) == 1:
            return error_response(503, "service_unavailable", "Try later")
        return json_response({"users": []})

    client, service = serve(handler)
    assert await client.list_end_users() == []
    calls = service.calls
    assert len(calls) == 2
    assert calls[0].path == "/api/v1/org/users"
    assert calls[0].headers["authorization"] == f"Bearer {KEY}"
    assert calls[0].headers["x-request-id"] == calls[1].headers["x-request-id"]
    for header in IDENTITY_SELECTORS:
        assert header not in calls[0].headers


async def test_a_service_conflict_preserves_safe_error_details() -> None:
    def handler(request, service):
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "revision_conflict",
                    "message": "The resource changed",
                    "details": {"currentRevision": 2},
                }
            },
            headers={"x-request-id": "request_from_service"},
        )

    client, _ = serve(handler)
    with pytest.raises(ConflictError) as caught:
        await client.list_end_users()
    error = caught.value
    assert isinstance(error, TiDBLinkApiError)
    assert isinstance(error, TiDBLinkError)
    assert error.status == 409
    assert error.code == "revision_conflict"
    assert error.request_id == "request_from_service"
    assert error.details == {"currentRevision": 2}


def test_rejects_a_user_api_key_without_exposing_it() -> None:
    api_key = "ti_user_" + "key_" + "x" * 43
    with pytest.raises(ValueError) as caught:
        OrganizationClient(base_url="https://agent.example.com", api_key=api_key)
    assert all(api_key not in text for text in rendered(caught.value))


async def test_accepts_only_the_frozen_organization_credential_grammar_before_dispatch() -> None:
    def handler(request, service):
        return json_response({"users": []})

    transport_service = serve(handler)[1]
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(transport_service))
    secret = "x" * 40 + "-_A"
    for api_key in [
        f"ti_org_key_{'x' * 42}",
        f"ti_org_key_{'x' * 44}",
        f"ti_org__{secret}",
        f"ti_org_{'a' * 65}_{secret}",
        f"ti_org_key_id_{secret}",
        f"ti_org_é_{secret}",
        f"ti_org_key_{'x' * 42}+",
        f"ti_org_key_{secret}\n",
        f"ti_org_key_\n{secret}",
        f"ti_org.key.{secret}",
        f"ti_user_key_{secret}",
        "ag9_oak." + "key." + "x" * 32,
        "",
        None,
        123,
    ]:
        expected = ValueError if isinstance(api_key, str) else TypeError
        with pytest.raises(expected) as caught:
            OrganizationClient(base_url=BASE_URL, api_key=api_key, http_client=http_client)  # type: ignore[arg-type]
        if isinstance(api_key, str) and api_key:
            assert all(api_key not in text for text in rendered(caught.value))
    assert transport_service.calls == []
    for key_id in ["A", "a" * 64]:
        api_key = f"ti_org_{key_id}_{secret}"
        client = OrganizationClient(base_url=BASE_URL, api_key=api_key, http_client=http_client)
        assert api_key not in repr(client)
        assert api_key not in str(client)
        await client.list_end_users()
        assert transport_service.calls[-1].headers["authorization"] == f"Bearer {api_key}"
    assert len(transport_service.calls) == 2


async def test_confirms_its_organization_without_identity_selectors() -> None:
    organization = {
        "organizationId": "org_1",
        "slug": "customer",
        "name": None,
        "createdAt": "2026-10-27T00:00:00.000Z",
        "updatedAt": "2026-10-27T00:00:00.000Z",
    }

    def handler(request, service):
        return json_response({"organization": organization})

    client, service = serve(handler)
    assert await client.get_organization() == organization
    call = service.calls[0]
    assert call.path == "/api/v1/org"
    assert call.headers["authorization"] == f"Bearer {KEY}"
    assert "x-agent9-project-id" not in call.headers
    assert "x-ti-organization-id" not in call.headers


async def test_native_end_users_are_created_listed_and_retrieved_with_their_external_mapping() -> None:
    user = {
        "userId": "user/1",
        "organizationId": "org_1",
        "displayName": "Customer",
        "externalId": "customer-1",
        "status": "active",
        "createdAt": "now",
        "updatedAt": "now",
    }

    def handler(request, service):
        call = service.calls[-1]
        if call.method == "POST":
            assert call.path == "/api/v1/org/users"
            assert call.json() == {"displayName": "Customer", "externalId": "customer-1"}
            assert call.headers["x-request-id"] == "create_customer_1"
            return json_response({"user": user}, 201)
        if call.path == "/api/v1/org/users":
            return json_response({"users": [user]})
        assert call.path == "/api/v1/org/users/user%2F1"
        return json_response({"user": user})

    client, _ = serve(handler)
    created = await client.create_end_user(
        display_name="Customer", external_id="customer-1", request_id="create_customer_1"
    )
    assert created == user
    assert await client.list_end_users() == [user]
    assert await client.get_end_user(created["userId"]) == user


async def test_end_user_creation_with_an_external_id_is_retried_with_the_same_request_identity() -> None:
    user = {
        "userId": "user_1",
        "organizationId": "org_1",
        "displayName": None,
        "externalId": "customer-1",
        "status": "active",
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:00.000Z",
    }

    def handler(request, service):
        if len(service.calls) == 1:
            return disconnect()
        return json_response({"user": user})

    client, service = serve(handler)
    assert await client.create_end_user(external_id="customer-1", request_id="create_1") == user
    assert [call.headers["x-request-id"] for call in service.calls] == ["create_1", "create_1"]


async def test_a_lost_end_user_creation_response_without_an_external_id_is_not_replayed() -> None:
    client, service = serve(disconnect)
    with pytest.raises(OutcomeUnknownError):
        await client.create_end_user(display_name="Customer", request_id="create_1")
    assert len(service.calls) == 1
    with pytest.raises(TypeError):
        await client.create_end_user(display_name="Customer", unexpected=True)  # type: ignore[call-arg]
    assert len(service.calls) == 1
    with pytest.raises(TiDBLinkConnectionError) as caught:
        await client.list_end_users()
    assert not isinstance(caught.value, OutcomeUnknownError)


async def test_unusable_creation_receipts_retain_the_request_identity_for_reconciliation() -> None:
    def handler(request, service):
        return json_response({"user": None}, 201, {"x-request-id": "create_receipt"})

    client, _ = serve(handler)
    with pytest.raises(OutcomeUnknownError) as caught:
        await client.create_end_user()
    assert caught.value.request_id == "create_receipt"


async def test_service_errors_retain_body_request_identity_and_uncertain_failures_retain_their_error() -> None:
    def handler(request, service):
        return error_response(
            503,
            "service_unavailable",
            "Service unavailable",
            requestId="body_request",
            details={"reason": "commit_unknown"},
        )

    client, _ = serve(handler)
    with pytest.raises(OutcomeUnknownError) as caught:
        await client.create_end_user(external_id="customer-1")
    error = caught.value
    assert error.request_id == "body_request"
    assert isinstance(error.cause, TiDBLinkApiError)
    assert error.cause.status == 503
    assert error.cause.code == "service_unavailable"
    assert error.cause.details == {"reason": "commit_unknown"}


async def test_redirects_never_send_an_api_key_to_another_recipient() -> None:
    def handler(request, service):
        if request.url.host == "recipient.test":
            return json_response({})
        return httpx.Response(307, headers={"location": "http://recipient.test/"})

    client, service = serve(handler)
    with pytest.raises(OutcomeUnknownError):
        await client.create_end_user()
    assert [call.url.host for call in service.calls] == ["tidblink.test"]


async def test_public_request_options_cannot_change_operations_or_enable_unsafe_replay() -> None:
    def handler(request, service):
        if request.method == "PATCH":
            return error_response(503, "service_unavailable", "Outcome uncertain")
        return json_response({"session": {"sessionId": "session_1"}}, headers={"etag": "model-v1"})

    client, service = serve(handler)
    session = client.user("user_1").session("session_1")
    for operation in [
        lambda: client.create_end_user(idempotent=True),
        lambda: session.refresh(method="DELETE", body={"unexpected": True}),
        lambda: session.get_realtime_connection(method="DELETE"),
        lambda: session.suggest_name(user_text="Name this", idempotent=True),
        lambda: session.rename("Name", retry="safe"),
    ]:
        with pytest.raises(TypeError):
            await operation()  # type: ignore[call-arg]
    assert service.calls == []
    with pytest.raises(OutcomeUnknownError):
        await session.set_model("next-model")
    assert [call.method for call in service.calls] == ["GET", "PATCH"]


async def test_organization_authority_selects_an_explicit_owner_across_resource_handles() -> None:
    def handler(request, service):
        return json_response(
            {
                "sessions": [],
                "templates": [],
                "models": [],
                "session": {"sessionId": "session_1"},
                "hasMore": False,
                "nextCursor": None,
            },
            headers={"etag": '"revision-1"'},
        )

    organization, service = serve(handler)
    for owner_id, expected in [
        ("", ValueError),
        ("../user", ValueError),
        ("user/other", ValueError),
        (None, TypeError),
    ]:
        with pytest.raises(expected):
            organization.user(owner_id)  # type: ignore[arg-type]
    assert service.calls == []
    owner = organization.user("user_1")
    await owner.create_session(template_id="template_1", idempotency_key="s-1")
    await owner.list_sessions(limit=2)
    await owner.session("session_1").refresh()
    await owner.list_agent_templates()
    await owner.list_models()
    await owner.list_realtime_models()
    assert [call.path for call in service.calls] == [
        "/api/v1/org/users/user_1/sessions",
        "/api/v1/org/users/user_1/sessions?limit=2",
        "/api/v1/org/users/user_1/sessions/session_1",
        "/api/v1/org/users/user_1/agent-templates",
        "/api/v1/org/users/user_1/agent-models",
        "/api/v1/org/users/user_1/realtime-models",
    ]
    assert all(call.headers["authorization"] == f"Bearer {KEY}" for call in service.calls)
    assert KEY not in repr(owner)
