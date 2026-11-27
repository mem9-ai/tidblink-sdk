"""Ported from the REST parts of typescript/test/realtime.test.mjs."""

from __future__ import annotations

import traceback
from typing import Any

import httpx
import pytest

from core_support import KEY, disconnect, session_for, user_for
from support import error_response, json_response
from tidblink_server import ConflictError, OutcomeUnknownError

SESSION_PATH = "/api/v1/org/users/user_1/sessions/session_1"


def realtime_connection(**overrides: Any) -> dict[str, Any]:
    return {
        "object": "realtime_connection",
        "id": "rtc_1",
        "sessionId": "session_1",
        "provider": {"name": "qwen-omni", "region": "cn-beijing", "model": "qwen-live"},
        "status": "connecting",
        "audio": {
            "input": {"encoding": "pcm16", "sampleRate": 16000, "channels": 1},
            "output": {"encoding": "pcm16", "sampleRate": 24000, "channels": 1},
        },
        "activeTaskTurnId": None,
        "usage": {"inputAudioSec": 0, "outputAudioSec": 0, "thoughtTokens": 0, "textTokens": 0},
        "metadata": {},
        "createdAt": "2026-10-27T12:00:00.000Z",
        "endedAt": None,
        **overrides,
    }


async def test_a_user_discovers_the_published_realtime_models_and_client_capabilities() -> None:
    catalog = {
        "defaultModel": "voice-default",
        "models": [
            {
                "id": "voice-default",
                "label": "Realtime Voice",
                "available": True,
                "isDefault": True,
                "languages": ["zh-CN", "en-US"],
                "endpointingModes": ["client", "server"],
                "audio": {
                    "input": [
                        {"encoding": "pcm16", "sampleRate": 16000, "channels": 1},
                        {"encoding": "opus", "sampleRate": 16000, "channels": 1, "packetDurationMs": 20},
                    ],
                    "output": [
                        {
                            "encoding": "opus",
                            "sampleRate": 24000,
                            "channels": 1,
                            "packetDurationMs": 20,
                            "bitrate": 32000,
                        }
                    ],
                },
            }
        ],
    }
    user, service = user_for(lambda request, service: json_response(catalog))
    assert await user.list_realtime_models() == catalog
    assert [(call.method, call.path, call.headers["authorization"]) for call in service.calls] == [
        ("GET", "/api/v1/org/users/user_1/realtime-models", f"Bearer {KEY}")
    ]


async def test_a_session_reads_and_conditionally_changes_its_independent_realtime_model_selection() -> None:
    def selection(**overrides: Any) -> dict[str, Any]:
        return {
            "realtimeModel": "voice-a",
            "realtimeModelRevision": 1,
            "realtimeModelEtag": '"session_1-realtime-1"',
            "realtimeLanguage": "en-US",
            **overrides,
        }

    state = {"updated": False}

    def handler(request, service):
        if request.method == "GET":
            return json_response({"session": selection()})
        if request.headers.get("if-match") == '"session_1-realtime-1"' and not state["updated"]:
            state["updated"] = True
            return json_response(
                {
                    "session": selection(
                        realtimeModel="voice-b",
                        realtimeModelRevision=2,
                        realtimeModelEtag='"session_1-realtime-2"',
                    )
                }
            )
        return error_response(412, "realtime_model_conflict", "The Realtime Model selection has changed.")

    session, service = session_for(handler)
    current = await session.get_realtime_model_selection()
    assert current == {
        "model": "voice-a",
        "revision": 1,
        "etag": '"session_1-realtime-1"',
        "language": "en-US",
    }
    updated = await session.set_realtime_model("voice-b", if_match=current["etag"])
    assert updated == {
        "model": "voice-b",
        "revision": 2,
        "etag": '"session_1-realtime-2"',
        "language": "en-US",
    }
    with pytest.raises(ConflictError) as caught:
        await session.set_realtime_model("voice-a", if_match=current["etag"])
    assert (caught.value.status, caught.value.code) == (412, "realtime_model_conflict")
    assert [
        (
            call.method,
            call.path,
            call.headers.get("if-match"),
            call.headers.get("idempotency-key"),
            call.json() if call.body else None,
        )
        for call in service.calls
    ] == [
        ("GET", SESSION_PATH, None, None, None),
        ("PATCH", f"{SESSION_PATH}/realtime-model", '"session_1-realtime-1"', None, {"realtimeModel": "voice-b"}),
        ("PATCH", f"{SESSION_PATH}/realtime-model", '"session_1-realtime-1"', None, {"realtimeModel": "voice-a"}),
    ]
    assert not any(call.path.startswith("/api/sessions") for call in service.calls)


@pytest.mark.parametrize(
    "language",
    [None, "fr-FR", {"language": "en-US"}],
    ids=["missing", "unsupported", "former-object-shape"],
)
async def test_set_realtime_model_treats_an_unusable_realtime_language_receipt_as_an_unknown_outcome(
    language: object,
) -> None:
    session_fields: dict[str, Any] = {
        "realtimeModel": "voice-b",
        "realtimeModelRevision": 2,
        "realtimeModelEtag": '"session_1-realtime-2"',
    }
    if language is not None:
        session_fields["realtimeLanguage"] = language
    session, _service = session_for(lambda request, service: json_response({"session": session_fields}))
    with pytest.raises(OutcomeUnknownError):
        await session.set_realtime_model("voice-b", if_match='"session_1-realtime-1"')


async def test_set_realtime_model_accepts_a_receipt_whose_session_has_realtime_disabled() -> None:
    session_fields = {
        "realtimeModel": "voice-b",
        "realtimeModelRevision": 2,
        "realtimeModelEtag": '"session_1-realtime-2"',
        "realtimeLanguage": None,
    }
    session, _service = session_for(lambda request, service: json_response({"session": session_fields}))
    assert await session.set_realtime_model("voice-b", if_match='"session_1-realtime-1"') == {
        "model": "voice-b",
        "revision": 2,
        "etag": '"session_1-realtime-2"',
        "language": None,
    }


async def test_a_session_prepares_a_realtime_connection_and_returns_its_one_time_connect_grant() -> None:
    output = {"encoding": "opus", "sampleRate": 24000, "channels": 1, "packetDurationMs": 20, "bitrate": 24000}
    connection = realtime_connection(
        audio={"input": {"encoding": "pcm16", "sampleRate": 16000, "channels": 1}, "output": output},
        metadata={"terminal": "kiosk-1"},
    )
    result = {
        "realtimeConnection": connection,
        "websocketUrl": "wss://voice.example/api/v1/org/users/user_1/sessions/session_1/realtime/ws",
        "connectToken": "rct_" + "a" * 43,
        "connectTokenExpiresAt": "2026-10-27T12:01:00.000Z",
    }
    session, service = session_for(lambda request, service: json_response(result, 201))
    assert await session.open_realtime_connection(audio={"output": output}, metadata={"terminal": "kiosk-1"}) == result
    assert [
        (call.method, call.path, call.headers.get("idempotency-key"), call.json()) for call in service.calls
    ] == [
        (
            "POST",
            f"{SESSION_PATH}/realtime",
            None,
            {"audio": {"output": output}, "metadata": {"terminal": "kiosk-1"}},
        )
    ]


async def test_a_session_inspects_its_newest_realtime_connection() -> None:
    connection = realtime_connection()
    session, service = session_for(lambda request, service: json_response({"realtimeConnection": connection}))
    assert await session.get_realtime_connection() == connection
    assert [(call.method, call.path) for call in service.calls] == [("GET", f"{SESSION_PATH}/realtime")]


async def test_a_session_requests_a_fresh_one_time_realtime_connect_token() -> None:
    grant = {
        "websocketUrl": "wss://voice.example/api/v1/org/users/user_1/sessions/session_1/realtime/ws",
        "connectToken": "rct_" + "b" * 43,
        "connectTokenExpiresAt": "2026-10-27T12:01:00.000Z",
    }
    session, service = session_for(lambda request, service: json_response(grant, 201))
    assert await session.create_realtime_connect_token() == grant
    assert [(call.method, call.path, call.headers.get("idempotency-key")) for call in service.calls] == [
        ("POST", f"{SESSION_PATH}/realtime/connect-tokens", None)
    ]


async def test_a_session_ends_and_returns_its_newest_realtime_connection_without_replaying_it() -> None:
    connection = realtime_connection(status="ended", endedAt="2026-10-27T12:02:00.000Z")
    session, service = session_for(lambda request, service: json_response({"realtimeConnection": connection}))
    assert await session.end_realtime_connection() == connection
    assert [(call.method, call.path, call.headers.get("idempotency-key")) for call in service.calls] == [
        ("DELETE", f"{SESSION_PATH}/realtime", None)
    ]


MUTATIONS = [
    (
        "Realtime Model selection",
        lambda session: session.set_realtime_model("voice-b", if_match='"realtime-v1"'),
        ("PATCH", f"{SESSION_PATH}/realtime-model"),
    ),
    (
        "Realtime Connection preparation",
        lambda session: session.open_realtime_connection(),
        ("POST", f"{SESSION_PATH}/realtime"),
    ),
    (
        "Realtime connect-token issuance",
        lambda session: session.create_realtime_connect_token(),
        ("POST", f"{SESSION_PATH}/realtime/connect-tokens"),
    ),
    (
        "Realtime Connection termination",
        lambda session: session.end_realtime_connection(),
        ("DELETE", f"{SESSION_PATH}/realtime"),
    ),
]


def rendered(error: BaseException) -> str:
    return " ".join([str(error), repr(error), "".join(traceback.format_exception(error))])


@pytest.mark.parametrize(("name", "invoke", "expected"), MUTATIONS, ids=[m[0] for m in MUTATIONS])
async def test_a_lost_mutation_response_is_not_replayed(name: str, invoke: Any, expected: tuple[str, str]) -> None:
    # The service received the complete request (MockTransport reads it) before the response was lost.
    session, service = session_for(disconnect)
    with pytest.raises(OutcomeUnknownError) as caught:
        await invoke(session)
    assert type(caught.value) is OutcomeUnknownError
    assert caught.value.request_id == service.calls[0].headers["x-request-id"]
    assert "rct_unreturned-secret" not in rendered(caught.value)
    assert len(service.calls) == 1
    assert (service.calls[0].method, service.calls[0].path) == expected
    assert "idempotency-key" not in service.calls[0].headers


@pytest.mark.parametrize(("name", "invoke", "expected"), MUTATIONS, ids=[m[0] for m in MUTATIONS])
async def test_an_unusable_successful_mutation_response_is_outcome_unknown(
    name: str, invoke: Any, expected: tuple[str, str]
) -> None:
    returned_token = "rct_" + "s" * 43

    def handler(request, service):
        headers = {"content-type": "application/json", "x-request-id": "service-receipt-id"}
        if name == "Realtime Model selection":
            return httpx.Response(204, headers=headers)
        if name == "Realtime connect-token issuance":
            return httpx.Response(
                201,
                headers=headers,
                json={
                    "websocketUrl": "not-a-websocket-url",
                    "connectToken": returned_token,
                    "connectTokenExpiresAt": "2026-10-27T12:01:00.000Z",
                },
            )
        return httpx.Response(201 if request.method == "POST" else 200, headers=headers, content=b"{}")

    session, service = session_for(handler)
    with pytest.raises(OutcomeUnknownError) as caught:
        await invoke(session)
    assert type(caught.value) is OutcomeUnknownError
    assert caught.value.request_id == "service-receipt-id"
    assert "rct_" not in rendered(caught.value)
    assert len(service.calls) == 1
