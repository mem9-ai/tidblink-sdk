"""Port of ``typescript/test/session-connect.test.mjs``."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from backend_support import API_KEY, FakeCore, Harness, harness, turn_event, until  # noqa: F401
from tidblink_server import BackendConnectionClosedError, BackendRequestError, TiDBLinkError
from tidblink_server.session import Session
from tidblink_server.session_connection import ClientToolHandler, SessionConnection, SessionConnectionEnd

CREATED_AT = "2026-11-13T00:00:00.000Z"


def session_of(harness: Harness, core: FakeCore, session_id: str = "sess_1") -> Session:
    return harness.client(core).user("user_1").session(session_id)


def event_frame(cursor: str, type_: str, data: dict[str, Any] | None = None, session_id: str = "sess_1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session.event",
        "params": {
            "sessionId": session_id,
            "cursor": cursor,
            "event": {"type": type_, "sessionId": session_id, "turnId": None, "createdAt": CREATED_AT, "data": data or {}},
        },
    }


def realtime(type_: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"rt_{type_}",
        "type": type_,
        "realtimeConnectionId": "rtc_1",
        "text": "too bright" if type_ == "input_transcript" else "Dimmed it.",
        "turnId": None,
        "createdAt": CREATED_AT,
        **extra,
    }


SET_LIGHT = ClientToolHandler(
    description="Turn the lamp on or off",
    parameters={"type": "object", "properties": {"on": {"type": "boolean"}}},
    run=lambda args, context: "ok",
)
LAMP_TOOLS = {"set_light": SET_LIGHT}


async def test_connect_attaches_the_session_for_execution_and_declares_its_client_tools(harness: Harness) -> None:
    core = await harness.core()
    conn = await session_of(harness, core).connect(tools=LAMP_TOOLS)
    assert isinstance(conn, SessionConnection)
    assert conn.session_id == "sess_1"
    assert conn.user_id == "user_1"
    assert conn.generation == 4
    assert conn.cursor == "10"
    peer = core.peers[0]
    assert peer.request.headers["authorization"] == f"Bearer {API_KEY}"
    assert peer.path == "/api/v1/org/backend/ws"
    assert peer.log[0]["params"] == {
        "userId": "user_1",
        "sessionId": "sess_1",
        "tools": [
            {
                "type": "function",
                "name": "set_light",
                "description": "Turn the lamp on or off",
                "parameters": {"type": "object", "properties": {"on": {"type": "boolean"}}},
            }
        ],
    }
    await conn.close()
    assert await conn.closed == SessionConnectionEnd("closed")
    assert peer.log[-1]["params"] == {"sessionId": "sess_1"}


async def test_handlers_may_be_plain_mappings(harness: Harness) -> None:
    core = await harness.core()
    tools = {"set_light": {"description": SET_LIGHT.description, "parameters": SET_LIGHT.parameters, "run": SET_LIGHT.run}}
    await session_of(harness, core).connect(tools=tools)
    assert core.peers[0].log[0]["params"]["tools"][0]["name"] == "set_light"


async def test_observing_connections_and_resume_cursors_are_sent_as_given_invalid_options_never_dispatch(
    harness: Harness,
) -> None:
    core = await harness.core()
    session = session_of(harness, core)
    await session.connect(observe=True, cursor="42")
    assert core.peers[0].log[0]["params"] == {
        "userId": "user_1",
        "sessionId": "sess_1",
        "kind": "observing",
        "cursor": "42",
    }
    cases: list[tuple[dict[str, Any], type[Exception]]] = [
        ({"observe": True, "tools": LAMP_TOOLS}, ValueError),
        ({"tools": {"set_light": {"description": "x", "parameters": {}}}}, TypeError),
        ({"tools": {"bad name": SET_LIGHT}}, ValueError),
        ({"tools": [SET_LIGHT]}, TypeError),
        ({"cursor": "abc"}, ValueError),
        ({"observe": True, "max_buffered_bytes": 0}, ValueError),
        ({"observe": True, "max_buffered_bytes": 1.5}, TypeError),
        ({"max_buffered_bytes": 1024}, ValueError),
        ({"on_event": "nope"}, TypeError),
        ({"on_file": {}}, TypeError),
        ({"unknown": True}, TypeError),
    ]
    for options, error in cases:
        with pytest.raises(error):
            await session.connect(**options)
    assert len(core.peers[0].log) == 1


async def test_events_arrive_in_order_through_their_callbacks_and_the_cursor_advances_after_them(
    harness: Harness,
) -> None:
    core = await harness.core()
    seen: list[tuple[Any, ...]] = []
    errors: list[BaseException] = []

    async def on_event(event: dict[str, Any]) -> None:
        await asyncio.sleep(0.005)
        seen.append(("event", event["type"], event["cursor"]))
        if event["cursor"] == "13":
            raise RuntimeError("application failure")

    conn = await session_of(harness, core).connect(
        on_event=on_event,
        on_transcript=lambda transcript: seen.append(("transcript", transcript["text"])),
        on_response=lambda response: seen.append(("response", response["text"], response["kind"])),
        on_cursor=lambda cursor: seen.append(("cursor", cursor)),
        on_error=errors.append,
    )
    peer = core.peers[0]
    peer.send(event_frame("11", "agent.session.realtime.input_transcript.completed", realtime("input_transcript")))
    peer.send(
        event_frame(
            "12",
            "agent.session.realtime.response.completed",
            realtime("response", responseId="r1", kind="chat", interrupted=False),
        )
    )
    peer.send(event_frame("13", "agent.session.future.event", {"anything": True}))
    peer.send(
        {"jsonrpc": "2.0", "method": "session.event", "params": {"sessionId": "sess_1", "cursor": "14", "event": {"type": "broken"}}}
    )
    peer.send(event_frame("15", "agent.session.template_switch.succeeded", {}))
    await until(lambda: conn.cursor == "15")
    assert seen == [
        ("event", "agent.session.realtime.input_transcript.completed", "11"),
        ("transcript", "too bright"),
        ("cursor", "11"),
        ("event", "agent.session.realtime.response.completed", "12"),
        ("response", "Dimmed it.", "chat"),
        ("cursor", "12"),
        ("event", "agent.session.future.event", "13"),
        ("cursor", "13"),
        ("event", "agent.session.template_switch.succeeded", "15"),
        ("cursor", "15"),
    ]
    assert len(errors) == 2
    assert str(errors[0]) == "application failure"
    assert isinstance(errors[1], TiDBLinkError)


async def test_on_file_receives_each_file_a_file_notification_announces_after_on_event_and_before_the_cursor(
    harness: Harness,
) -> None:
    core = await harness.core()
    seen: list[tuple[Any, ...]] = []
    errors: list[BaseException] = []

    async def on_file(file: dict[str, Any]) -> None:
        await asyncio.sleep(0.005)
        seen.append(("file", file))
        if file["name"] == "broken.bin":
            raise RuntimeError("application failure")

    conn = await session_of(harness, core).connect(
        on_event=lambda event: seen.append(("event", event["cursor"])),
        on_file=on_file,
        on_cursor=lambda cursor: seen.append(("cursor", cursor)),
        on_error=errors.append,
    )
    firmware = {
        "path": "build/firmware.bin",
        "name": "firmware.bin",
        "version": "v1AAAAAAAAAAAAAAAAAAAA",
        "byteSize": 4,
        "updatedAt": CREATED_AT,
    }

    def step(cursor: str, **payload: Any) -> dict[str, Any]:
        return turn_event(
            cursor,
            "agent.session.turn.item.added",
            "operation_step",
            {"operationId": f"op_{cursor}", "label": "Notifying file", "category": "tool_call", **payload},
        )

    peer = core.peers[0]
    peer.send(step("11", status="running"))
    peer.send(step("12", status="succeeded", label="Completed notifying file", file=firmware))
    peer.send(step("13", status="succeeded", file={**firmware, "byteSize": "4"}))
    peer.send(step("14", status="succeeded", file={**firmware, "name": "broken.bin"}))
    await until(lambda: conn.cursor == "14")
    assert seen == [
        ("event", "11"),
        ("cursor", "11"),
        ("event", "12"),
        ("file", firmware),
        ("cursor", "12"),
        # A malformed file reaches on_event only.
        ("event", "13"),
        ("cursor", "13"),
        ("event", "14"),
        ("file", {**firmware, "name": "broken.bin"}),
        ("cursor", "14"),
    ]
    assert len(errors) == 1
    assert str(errors[0]) == "application failure"


async def test_without_on_error_callback_errors_go_to_the_event_loops_exception_handler(harness: Harness) -> None:
    core = await harness.core()

    def on_event(event: dict[str, Any]) -> None:
        raise RuntimeError("application failure")

    conn = await session_of(harness, core).connect(on_event=on_event)
    core.peers[0].send(event_frame("11", "agent.session.turn.started"))
    await until(lambda: conn.cursor == "11" and harness.unhandled)
    assert str(harness.unhandled[0]["exception"]) == "application failure"
    harness.unhandled.clear()


async def test_after_a_lost_connection_the_session_reattaches_from_the_last_processed_event_and_skips_replays(
    harness: Harness,
) -> None:
    core = await harness.core()
    cursors: list[str] = []
    conn = await session_of(harness, core).connect(tools=LAMP_TOOLS, on_cursor=cursors.append)
    first = core.peers[0]
    first.send(event_frame("11", "agent.session.turn.started"))
    await until(lambda: conn.cursor == "11")
    first.destroy()
    second = await core.connection(1)
    await until(lambda: len(second.log) == 1)
    assert second.log[0]["params"] == {
        "userId": "user_1",
        "sessionId": "sess_1",
        "tools": first.log[0]["params"]["tools"],
        "cursor": "11",
    }
    # The core replays after the cursor; a duplicate from a race is skipped.
    second.send(event_frame("11", "agent.session.turn.started"))
    second.send(event_frame("12", "agent.session.turn.completed"))
    await until(lambda: conn.cursor == "12")
    assert cursors == ["11", "12"]
    assert conn.generation == 4


async def test_a_connection_taken_over_elsewhere_ends_as_superseded_and_stops_delivering(harness: Harness) -> None:
    core = await harness.core()
    seen: list[str] = []
    conn = await session_of(harness, core).connect(on_event=lambda event: seen.append(event["cursor"]))
    peer = core.peers[0]
    peer.send({"jsonrpc": "2.0", "method": "session.detached", "params": {"sessionId": "sess_1", "reason": "superseded"}})
    assert await conn.closed == SessionConnectionEnd("superseded")
    peer.send(event_frame("11", "agent.session.turn.started"))
    await asyncio.sleep(0.05)
    assert seen == []
    await conn.close()
    assert [m for m in peer.log if m["method"] == "session.detach"] == []


async def test_one_connection_per_session_per_client_refusals_and_a_revoked_key_surface_as_errors(
    harness: Harness,
) -> None:
    core = await harness.core(
        respond=lambda message, peer, index: (
            {"error": {"code": -32000, "message": "Refused", "data": {"reason": "session_not_found"}}}
            if message.get("params", {}).get("sessionId") == "missing"
            else None
        )
    )
    client = harness.client(core)
    conn = await client.user("user_1").session("sess_1").connect()
    with pytest.raises(TiDBLinkError, match="already connected"):
        await client.user("user_1").session("sess_1").connect()
    with pytest.raises(BackendRequestError) as refused:
        await client.user("user_1").session("missing").connect()
    assert refused.value.reason == "session_not_found"
    core.peers[0].close(4001, "revoked")
    end = await conn.closed
    assert end.reason == "error"
    assert isinstance(end.error, BackendConnectionClosedError)
    assert end.error.retryable is False


async def test_organization_client_close_ends_its_connections_as_closed_and_refuses_new_ones(harness: Harness) -> None:
    core = await harness.core()
    client = harness.client(core)
    conn = await client.user("user_1").session("sess_1").connect()
    await client.close()
    assert await conn.closed == SessionConnectionEnd("closed")
    assert any(m["method"] == "session.detach" for m in core.peers[0].log)
    with pytest.raises(TiDBLinkError, match="closed"):
        await client.user("user_1").session("sess_2").connect()


async def test_a_connection_whose_callbacks_fall_behind_drops_events_then_has_the_core_send_them_again(
    harness: Harness,
) -> None:
    # The core stand-in keeps every event it sent, and an attach with a cursor replays those after it.
    sent: list[dict[str, Any]] = []

    def respond(message: dict[str, Any], peer: Any, index: int) -> Any:
        if message["method"] != "session.attach":
            return None
        after = int(message["params"].get("cursor", "10"))

        async def replay() -> None:
            await asyncio.sleep(0)
            for frame in list(sent):
                if int(frame["params"]["cursor"]) > after:
                    peer.send(frame)

        tasks.append(asyncio.ensure_future(replay()))
        return {"result": {"generation": 4, "cursor": str(len(sent) + 10)}}

    tasks: list[asyncio.Task[None]] = []
    core = await harness.core(respond=respond)
    delivered: list[str] = []
    gate = asyncio.Event()

    def frame(n: int) -> dict[str, Any]:
        return event_frame(str(10 + n), "agent.session.turn.progress", {"text": "x" * 200})

    size = len(json.dumps(frame(1), separators=(",", ":")))

    async def on_event(event: dict[str, Any]) -> None:
        delivered.append(event["cursor"])
        await gate.wait()

    conn = await session_of(harness, core).connect(observe=True, max_buffered_bytes=size * 3, on_event=on_event)
    peer = core.peers[0]
    for n in range(1, 11):
        sent.append(frame(n))
        peer.send(frame(n))
    await until(lambda: len(delivered) == 1)
    await asyncio.sleep(0.05)
    # Only what fits is waiting; nothing was attached again yet.
    assert len(delivered) == 1
    assert [m for m in peer.log if m["method"] == "session.attach"].__len__() == 1
    gate.set()
    await until(lambda: len(delivered) == 10)
    assert delivered == [str(11 + n) for n in range(10)]
    # Each re-attach resumes after the last event the callbacks completed; the replay itself may
    # outrun them again.
    attaches = [m for m in peer.log if m["method"] == "session.attach"]
    assert len(attaches) >= 2
    assert attaches[1]["params"]["cursor"] == "13"
    for attach in attaches[1:]:
        assert int(attach["params"]["cursor"]) >= 13
    assert conn.cursor == "20"
    await conn.close()
    await asyncio.gather(*tasks)


async def test_events_that_arrive_together_with_the_attach_answer_are_delivered_after_connect_returns(
    harness: Harness,
) -> None:
    def respond(message: dict[str, Any], peer: Any, index: int) -> Any:
        if message["method"] != "session.attach":
            return None
        # The answer and the replay leave together, as a fast core sends them.
        peer.send({"jsonrpc": "2.0", "id": message["id"], "result": {"generation": 4, "cursor": "13"}})
        for cursor in ("11", "12", "13"):
            peer.send(event_frame(cursor, "agent.session.turn.progress"))
        return "silent"

    core = await harness.core(respond=respond)
    delivered: list[tuple[str, str]] = []
    conn: SessionConnection | None = None
    # The callbacks run only once connect() has handed over the connection.
    conn = await session_of(harness, core).connect(
        observe=True,
        cursor="10",
        on_event=lambda event: delivered.append((event["cursor"], conn.session_id)),  # type: ignore[union-attr]
    )
    await until(lambda: len(delivered) == 3)
    assert delivered == [("11", "sess_1"), ("12", "sess_1"), ("13", "sess_1")]


async def test_an_executing_connection_never_attaches_again_to_catch_up(harness: Harness) -> None:
    core = await harness.core()
    delivered: list[str] = []
    gate = asyncio.Event()

    async def on_event(event: dict[str, Any]) -> None:
        delivered.append(event["cursor"])
        await gate.wait()

    await session_of(harness, core).connect(tools=LAMP_TOOLS, on_event=on_event)
    peer = core.peers[0]
    big = "x" * (4 * 1024 * 1024)
    for n in range(1, 5):
        peer.send(event_frame(str(10 + n), "agent.session.turn.progress", {"text": big}))
    await until(lambda: len(delivered) == 1)
    gate.set()
    await until(lambda: len(delivered) == 4)
    assert len([m for m in peer.log if m["method"] == "session.attach"]) == 1


async def test_a_catch_up_the_core_refuses_because_the_session_is_gone_ends_the_connection_as_deleted(
    harness: Harness,
) -> None:
    attaches = 0

    def respond(message: dict[str, Any], peer: Any, index: int) -> Any:
        nonlocal attaches
        if message["method"] != "session.attach":
            return None
        attaches += 1
        if attaches == 1:
            return {"result": {"generation": 4, "cursor": "10"}}
        return {"error": {"code": -32000, "message": "gone", "data": {"reason": "session_not_found"}}}

    core = await harness.core(respond=respond)
    gate = asyncio.Event()

    def frame(n: int) -> dict[str, Any]:
        return event_frame(str(10 + n), "agent.session.turn.progress", {"text": "y" * 200})

    async def on_event(event: dict[str, Any]) -> None:
        await gate.wait()

    conn = await session_of(harness, core).connect(
        observe=True, max_buffered_bytes=len(json.dumps(frame(1), separators=(",", ":"))) * 2, on_event=on_event
    )
    peer = core.peers[0]
    for n in range(1, 6):
        peer.send(frame(n))
    await asyncio.sleep(0.05)
    gate.set()
    assert await asyncio.wait_for(conn.closed, 5) == SessionConnectionEnd("session_deleted")
    assert attaches == 2
    await until(lambda: peer.log and peer.log[-1]["method"] == "session.detach")


async def test_websocket_connect_replaces_how_backend_connections_are_opened(harness: Harness) -> None:
    from tidblink_server import OrganizationClient
    from tidblink_server._backend_connection import default_websocket_connect

    core = await harness.core()
    opened: list[tuple[str, list[str], dict[str, str]]] = []

    async def connect(url: str, *, subprotocols: Any, headers: Any) -> Any:
        opened.append((url, list(subprotocols), dict(headers)))
        return await default_websocket_connect(url, subprotocols=subprotocols, headers=headers)

    client = OrganizationClient(
        base_url=core.base_url, api_key=API_KEY, http_client=harness.http_client(), websocket_connect=connect
    )
    harness.clients.append(client)
    await client.user("user_1").session("sess_1").connect()
    assert opened == [(core.url, ["tidb-link.backend.v1"], {"authorization": f"Bearer {API_KEY}"})]
