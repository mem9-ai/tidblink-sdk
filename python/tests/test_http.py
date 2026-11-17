"""The HTTP boundary: the non-file parts of typescript/test/fetch.test.mjs, and the
retry, redirect, outcome and request-identity rules of ``http.ts``."""

from __future__ import annotations

import uuid

import httpx
import pytest

from core_support import KEY, disconnect
from support import BASE_URL, Service, error_response, json_response, serve
from tidblink_server import (
    ConflictError,
    OrganizationClient,
    OutcomeUnknownError,
    TiDBLinkApiError,
    TiDBLinkConnectionError,
    TiDBLinkError,
)


async def test_an_injected_http_client_carries_every_request_and_stays_open() -> None:
    # File transfers through the injected client are covered with the files port.
    service = Service(lambda request, service: json_response({"users": []}))
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(service))
    async with OrganizationClient(base_url="https://tidb-link.test", api_key=KEY, http_client=http_client) as client:
        assert await client.list_end_users() == []
        await client.user("user_1").list_models()
    assert [f"{call.method} {call.url}" for call in service.calls] == [
        "GET https://tidb-link.test/api/v1/org/users",
        "GET https://tidb-link.test/api/v1/org/users/user_1/agent-models",
    ]
    assert all(call.headers["authorization"] == f"Bearer {KEY}" for call in service.calls)
    # The caller owns an injected client: closing the OrganizationClient leaves it open.
    assert not http_client.is_closed
    await http_client.aclose()


async def test_an_owned_http_client_is_closed_with_the_organization_client() -> None:
    client = OrganizationClient(base_url=BASE_URL, api_key=KEY)
    transport = client._http.client  # noqa: SLF001
    await client.close()
    assert transport.is_closed


def test_http_client_must_be_an_async_httpx_client() -> None:
    for http_client in ["fetch", httpx.Client()]:
        with pytest.raises(TypeError):
            OrganizationClient(base_url=BASE_URL, api_key=KEY, http_client=http_client)  # type: ignore[arg-type]


async def test_safe_reads_retry_retryable_failures_with_one_request_identity_then_report_the_last() -> None:
    for status in [408, 429, 500, 502, 503, 504]:
        client, service = serve(lambda request, service, status=status: error_response(status, "busy", "Busy"))
        with pytest.raises(TiDBLinkApiError) as caught:
            await client.list_end_users(request_id="read-1")
        assert caught.value.status == status
        assert not isinstance(caught.value, OutcomeUnknownError)
        assert [call.headers["x-request-id"] for call in service.calls] == ["read-1"] * 3

    client, service = serve(disconnect)
    with pytest.raises(TiDBLinkConnectionError) as unreachable:
        await client.list_end_users()
    assert len(service.calls) == 3
    assert len({call.headers["x-request-id"] for call in service.calls}) == 1
    assert unreachable.value.request_id == service.calls[0].headers["x-request-id"]
    assert isinstance(unreachable.value.cause, httpx.TransportError)

    client, service = serve(lambda request, service: error_response(400, "validation_error", "Bad"))
    with pytest.raises(TiDBLinkApiError):
        await client.list_end_users()
    assert len(service.calls) == 1


async def test_mutations_without_a_stable_identity_are_sent_once() -> None:
    client, service = serve(lambda request, service: error_response(429, "rate_limited", "Slow down"))
    with pytest.raises(TiDBLinkApiError) as caught:
        await client.create_end_user(display_name="Customer")
    assert not isinstance(caught.value, OutcomeUnknownError)
    assert caught.value.status == 429
    assert len(service.calls) == 1


async def test_retry_after_and_details_are_kept_on_service_errors() -> None:
    client, _ = serve(
        lambda request, service: httpx.Response(
            400,
            json={"error": {"code": "validation_error", "message": "Bad", "details": None}},
            headers={"retry-after": "3"},
        )
    )
    with pytest.raises(TiDBLinkApiError) as caught:
        await client.list_end_users()
    assert caught.value.retry_after == "3"
    assert caught.value.details is None
    assert str(caught.value) == "Bad"

    client, _ = serve(lambda request, service: httpx.Response(400, content=b"not json"))
    with pytest.raises(TiDBLinkApiError) as fallback:
        await client.list_end_users()
    assert fallback.value.code == "http_error"
    assert str(fallback.value) == "TiDB Link request failed with status 400"


async def test_redirects_are_refused_even_by_a_client_that_follows_them() -> None:
    service = Service(
        lambda request, service: json_response({"users": []})
        if request.url.host == "recipient.test"
        else httpx.Response(302, headers={"location": "http://recipient.test/api/v1/org/users"})
    )
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(service), follow_redirects=True)
    client = OrganizationClient(base_url=BASE_URL, api_key=KEY, http_client=http_client)
    with pytest.raises(TiDBLinkConnectionError) as caught:
        await client.list_end_users()
    assert not isinstance(caught.value, OutcomeUnknownError)
    assert {call.url.host for call in service.calls} == {"tidblink.test"}
    assert len(service.calls) == 3


async def test_408_and_5xx_mutation_failures_are_outcome_unknown_and_other_refusals_are_definite() -> None:
    for status in [408, 500, 502, 503, 504]:
        client, service = serve(
            lambda request, service, status=status: error_response(status, "uncertain", "Uncertain")
        )
        with pytest.raises(OutcomeUnknownError) as caught:
            await client.user("user_1").session("session_1").rename("Name", request_id="rename-1")
        assert isinstance(caught.value.cause, TiDBLinkApiError)
        assert caught.value.cause.status == status
        assert caught.value.request_id == "rename-1"
        assert len(service.calls) == 1
    for status, error_class in [
        (400, TiDBLinkApiError),
        (404, TiDBLinkApiError),
        (409, ConflictError),
        (412, ConflictError),
        (429, TiDBLinkApiError),
    ]:
        client, _ = serve(lambda request, service, status=status: error_response(status, "refused"))
        with pytest.raises(error_class) as refused:
            await client.user("user_1").session("session_1").rename("Name")
        assert type(refused.value) is error_class
        assert refused.value.status == status


async def test_request_identity_prefers_the_response_header_then_the_error_body_then_the_sent_identity() -> None:
    def handler(request, service):
        mode = request.url.params.get("mode")
        if mode == "header":
            return httpx.Response(
                404,
                json={"error": {"code": "missing", "message": "Missing", "requestId": "from_body"}},
                headers={"x-request-id": "from_header"},
            )
        if mode == "body":
            return error_response(404, "missing", "Missing", requestId="from_body")
        return error_response(404, "missing", "Missing")

    client, service = serve(handler)
    http = client._http  # noqa: SLF001
    for mode, expected in [("header", "from_header"), ("body", "from_body"), ("sent", "sent_1")]:
        with pytest.raises(TiDBLinkApiError) as caught:
            await http.get(f"/users?mode={mode}", request_id="sent_1")
        assert caught.value.request_id == expected
    # Without a caller identity, the SDK sends a fresh UUID.
    with pytest.raises(TiDBLinkApiError) as generated:
        await client.list_end_users()
    sent = service.calls[-1].headers["x-request-id"]
    assert uuid.UUID(sent)
    assert generated.value.request_id == sent


async def test_a_successful_response_identity_replaces_the_sent_one() -> None:
    client, _ = serve(
        lambda request, service: json_response({"user": None}, 201, {"x-request-id": "service_identity"})
    )
    with pytest.raises(OutcomeUnknownError) as caught:
        await client.create_end_user(request_id="sent_1")
    assert caught.value.request_id == "service_identity"


async def test_unusable_request_identities_fail_before_dispatch() -> None:
    client, service = serve(lambda request, service: json_response({"users": []}))
    for request_id in ["", "_leading", "has space", "x" * 65, "é"]:
        with pytest.raises(ValueError, match="request_id must be 1-64 safe characters"):
            await client.list_end_users(request_id=request_id)
    await client.list_end_users(request_id="a" * 64)
    assert len(service.calls) == 1


async def test_invalid_json_is_an_error_for_reads_and_an_unknown_outcome_for_mutations() -> None:
    client, _ = serve(lambda request, service: httpx.Response(200, content=b"{"))
    with pytest.raises(TiDBLinkError, match="invalid JSON") as read:
        await client.list_end_users()
    assert not isinstance(read.value, OutcomeUnknownError)
    with pytest.raises(OutcomeUnknownError, match="unusable response"):
        await client.create_end_user()


def test_base_urls_must_be_http_urls_without_credentials_query_or_fragment() -> None:
    for base_url in [
        "ftp://tidblink.test",
        "tidblink.test",
        "http://",
        "https://user:secret@tidblink.test",
        "https://tidblink.test?x=1",
        "https://tidblink.test/#top",
    ]:
        with pytest.raises(ValueError):
            OrganizationClient(base_url=base_url, api_key=KEY)
    with pytest.raises(TypeError):
        OrganizationClient(base_url=None, api_key=KEY)  # type: ignore[arg-type]


async def test_a_base_url_keeps_its_path_prefix_without_a_trailing_slash() -> None:
    for base_url in ["https://Gateway.test/tidb-link/", "https://gateway.test/tidb-link"]:
        service = Service(lambda request, service: json_response({"users": []}))
        client = OrganizationClient(
            base_url=base_url, api_key=KEY, http_client=httpx.AsyncClient(transport=httpx.MockTransport(service))
        )
        await client.list_end_users()
        assert str(service.calls[0].url) == "https://gateway.test/tidb-link/api/v1/org/users"
