"""Port of ``typescript/test/session-send.test.mjs``."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable

import httpx
import pytest

from backend_support import (  # noqa: F401
    AT,
    DONE,
    FakeCore,
    Harness,
    HttpCall,
    Peer,
    by_method,
    draft,
    finished,
    harness,
    idle,
    json_response,
    receipt,
    rest,
    settled,
)
from tidblink_server import (
    BackendRequestError,
    InputNotAppliedError,
    OutcomeUnknownError,
    TiDBLinkError,
    TurnFailedError,
    TurnWaitTimeoutError,
)
from tidblink_server.session import Session

def later(delay: float, *actions: Callable[[], Any]) -> None:
    async def run() -> None:
        await asyncio.sleep(delay)
        for action in actions:
            action()

    _background.add(asyncio.ensure_future(run()))


_background: set[asyncio.Task[None]] = set()


def session_of(
    harness: Harness, core: FakeCore, http: Any = None, calls: list[HttpCall] | None = None
) -> Session:
    return harness.client(core, http, calls).user("user_1").session("sess_1")


async def test_send_submits_over_the_connection_streams_drafts_and_returns_the_settled_answer(harness: Harness) -> None:
    calls: list[HttpCall] = []

    def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
        if message["method"] != "session.send":
            return None
        # Events of the Turn may precede the receipt.
        peer.send(draft("11", "Dim"))
        peer.send(draft("12", "Dimmed to"))
        later(
            0.02,
            lambda: peer.send(draft("13", "other turn", "turn_9")),
            lambda: peer.send(draft("14", "Dimmed to 40%.")),
            lambda: peer.send(finished("15")),
            lambda: peer.send(idle("16")),
        )
        return {"result": {"interaction": receipt(), "replayed": False}}

    core = await harness.core(respond=respond)
    conn = await session_of(harness, core, rest(), calls).connect()
    drafts: list[str] = []
    reply = await conn.send("Too bright", client_event_id="msg_1", billing_tag=None, on_progress=drafts.append)
    assert reply.type == "answer"
    assert reply.text == "Dimmed to 40%."
    assert reply.turn_id == "turn_1"
    assert reply.input_id == "msg_1"
    assert drafts == ["Dim", "Dimmed to", "Dimmed to 40%."]
    assert by_method(core.peers[0], "session.send")[0]["params"] == {
        "sessionId": "sess_1",
        "clientEventId": "msg_1",
        "data": {"text": "Too bright", "billingTag": None},
    }
    # One read of the settled interaction and one of the Turn; no polling while it ran.
    assert [str(call) for call in calls] == [
        "GET /api/v1/org/users/user_1/sessions/sess_1/interactions/msg_1",
        "GET /api/v1/org/users/user_1/sessions/sess_1/turns/turn_1",
    ]


async def test_a_send_whose_answer_was_lost_goes_out_again_with_the_same_client_event_id_on_the_next_connection(
    harness: Harness,
) -> None:
    def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
        if message["method"] != "session.send":
            return None
        if index == 0:
            peer.destroy()
            return "silent"
        later(0.01, lambda: peer.send(finished("20")), lambda: peer.send(idle("21")))
        return {"result": {"interaction": receipt(), "replayed": True}}

    core = await harness.core(respond=respond)
    conn = await session_of(harness, core, rest()).connect()
    reply = await conn.send("Too bright", client_event_id="msg_1")
    first = by_method(core.peers[0], "session.send")[0]
    again = by_method(core.peers[1], "session.send")[0]
    assert again["params"]["clientEventId"] == first["params"]["clientEventId"]
    assert reply.text == "Dimmed to 40%."


async def test_refusals_rejected_messages_failed_turns_and_timeouts_surface_like_turn(harness: Harness) -> None:
    mode = ""

    def http(request: httpx.Request) -> httpx.Response:
        if mode == "failed":
            return rest(settled(result={"status": "failed", "completedAt": DONE}))(request)
        return rest()(request)

    def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
        if message["method"] != "session.send":
            return None
        if mode == "refused":
            return {
                "error": {
                    "code": -32000,
                    "message": "The Session is read-only",
                    "data": {"reason": "session_read_only", "details": {"since": AT}},
                }
            }
        if mode == "rejected":
            return {
                "result": {
                    "interaction": receipt(mode="rejected", rejection={"code": "interaction_admission_closed"}),
                    "replayed": False,
                }
            }
        if mode == "failed":
            later(0.01, lambda: peer.send(finished("30")), lambda: peer.send(idle("31")))
        return {"result": {"interaction": receipt(), "replayed": False}}

    core = await harness.core(respond=respond)
    conn = await session_of(harness, core, http).connect()
    mode = "refused"
    with pytest.raises(BackendRequestError) as refused:
        await conn.send("x")
    assert refused.value.reason == "session_read_only"
    assert refused.value.details["since"] == AT
    mode = "rejected"
    with pytest.raises(InputNotAppliedError):
        await conn.send("x", client_event_id="msg_1")
    mode = "failed"
    with pytest.raises(TurnFailedError):
        await conn.send("x", client_event_id="msg_1")
    mode = "running"
    with pytest.raises(TurnWaitTimeoutError) as timed_out:
        await conn.send("x", client_event_id="msg_1", timeout=0.05)
    assert timed_out.value.input_id == "msg_1"
    assert timed_out.value.turn_id == "turn_1"
    bad: list[tuple[tuple[Any, ...], dict[str, Any], type[Exception]]] = [
        (([],), {}, ValueError),
        (("x",), {"client_event_id": "bad id"}, ValueError),
        (("x",), {"unknown": 1}, TypeError),
        (("x",), {"on_progress": 1}, TypeError),
    ]
    for args, options, error in bad:
        with pytest.raises(error):
            await conn.send(*args, **options)


async def test_a_send_after_the_connection_ended_is_refused_and_one_cut_off_mid_send_is_uncertain(
    harness: Harness,
) -> None:
    def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
        if message["method"] != "session.send":
            return None
        peer.send({"jsonrpc": "2.0", "method": "session.detached", "params": {"sessionId": "sess_1", "reason": "superseded"}})
        return "silent"

    core = await harness.core(respond=respond)
    conn = await session_of(harness, core).connect()
    with pytest.raises(OutcomeUnknownError) as uncertain:
        await conn.send("x", client_event_id="msg_1", timeout=5)
    assert uncertain.value.input_id == "msg_1"
    await conn.closed
    with pytest.raises(TiDBLinkError, match="superseded"):
        await conn.send("x")


async def test_a_generated_client_event_id_is_a_uuid(harness: Harness) -> None:
    core = await harness.core(
        respond=lambda message, peer, index: (
            {"error": {"code": -32000, "message": "Read-only", "data": {"reason": "session_read_only"}}}
            if message["method"] == "session.send"
            else None
        )
    )
    conn = await session_of(harness, core).connect()
    with pytest.raises(BackendRequestError):
        await conn.send("x")
    assert re.fullmatch(r"[0-9a-f-]{36}", by_method(core.peers[0], "session.send")[0]["params"]["clientEventId"])


async def test_realtime_grant_opens_the_realtime_connection_or_issues_a_token_for_a_live_one(harness: Harness) -> None:
    calls: list[HttpCall] = []
    live = False
    grant = {
        "websocketUrl": "wss://voice.example/realtime/ws",
        "connectToken": "rct_" + "a" * 43,
        "connectTokenExpiresAt": "2026-11-13T00:01:00.000Z",
    }
    connection = {
        "object": "realtime_connection",
        "id": "rtc_1",
        "sessionId": "sess_1",
        "provider": {"name": "qwen-omni", "region": "cn-beijing", "model": "qwen-live"},
        "status": "connecting",
        "audio": {
            "input": {"encoding": "pcm16", "sampleRate": 16000, "channels": 1},
            "output": {"encoding": "pcm16", "sampleRate": 16000, "channels": 1},
        },
        "activeTaskTurnId": None,
        "usage": {"inputAudioSec": 0, "outputAudioSec": 0, "thoughtTokens": 0, "textTokens": 0},
        "metadata": {},
        "createdAt": AT,
        "endedAt": None,
    }

    def http(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/realtime") and live:
            return json_response({"error": {"code": "realtime_connection_live", "message": "Live"}}, 409)
        if request.url.path.endswith("/realtime"):
            return json_response({"realtimeConnection": connection, **grant}, 201)
        return json_response({**grant, "connectToken": "rct_" + "b" * 43}, 201)

    core = await harness.core()
    conn = await session_of(harness, core, http, calls).connect()
    audio = {"input": {"encoding": "pcm16", "sampleRate": 16000, "channels": 1}}
    assert await conn.realtime_grant(audio=audio) == grant
    live = True
    assert (await conn.realtime_grant())["connectToken"] == "rct_" + "b" * 43
    assert [(call.method, call.path, call.body) for call in calls] == [
        ("POST", "/api/v1/org/users/user_1/sessions/sess_1/realtime", json.dumps({"audio": audio}, separators=(",", ":")).encode()),
        ("POST", "/api/v1/org/users/user_1/sessions/sess_1/realtime", b"{}"),
        ("POST", "/api/v1/org/users/user_1/sessions/sess_1/realtime/connect-tokens", b""),
    ]
    with pytest.raises(TypeError):
        await conn.realtime_grant(voice="x")  # type: ignore[call-arg]


async def test_send_returns_only_once_the_session_released_the_turn_or_with_the_result_if_the_release_is_late(
    harness: Harness,
) -> None:
    calls: list[HttpCall] = []
    release = True

    def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
        if message["method"] != "session.send":
            return None
        base = 40 if release else 50
        if release:
            later(0.01, lambda: peer.send(finished(str(base))), lambda: later(0.08, lambda: peer.send(idle(str(base + 1)))))
        else:
            later(0.01, lambda: peer.send(finished(str(base))))
        return {"result": {"interaction": receipt(), "replayed": False}}

    core = await harness.core(respond=respond)
    conn = await session_of(harness, core, rest(), calls).connect()
    loop = asyncio.get_running_loop()
    started = loop.time()
    await conn.send("Too bright", client_event_id="msg_1")
    assert loop.time() - started >= 0.08, "waited for agent.session.idle"
    assert len(calls) == 2, "the result is read after the release"
    release = False
    reply = await conn.send("Too bright", client_event_id="msg_1", timeout=0.3)
    assert reply.text == "Dimmed to 40%."


async def test_a_send_whose_deadline_passes_while_its_answer_is_outstanding_has_an_unknown_outcome(
    harness: Harness,
) -> None:
    def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
        # The core received the message but has not answered yet.
        return "silent" if message["method"] == "session.send" else None

    core = await harness.core(respond=respond)
    conn = await session_of(harness, core).connect()
    with pytest.raises(TurnWaitTimeoutError) as timed_out:
        await conn.send("x", client_event_id="msg_1", timeout=0.05)
    assert timed_out.value.input_id == "msg_1"
    assert timed_out.value.session_id == "sess_1"
    assert isinstance(timed_out.value.__cause__, TimeoutError)
