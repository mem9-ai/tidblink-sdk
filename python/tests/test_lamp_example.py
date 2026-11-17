"""Required tests 1-4 of ``examples/lamp/README.md`` (``typescript/test/lamp-example.test.mjs``)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend_support import (  # noqa: F401
    API_KEY,
    FakeCore,
    Harness,
    Peer,
    by_method,
    finished,
    harness,
    idle,
    json_response,
    receipt,
    rest,
    until,
)
from examples.lamp_backend import LampBackend, LampBinding, MemoryStore, create_lamp_backend

#: The lamp's tools every SDK's lamp example declares.
LAMP_TOOL_DECLARATIONS = json.loads(
    (Path(__file__).resolve().parents[2] / "examples" / "lamp" / "tools.json").read_text("utf-8")
)

GRANT = {
    "websocketUrl": "wss://voice.example/realtime/ws",
    "connectToken": "rct_" + "a" * 43,
    "connectTokenExpiresAt": "2026-11-13T00:01:00.000Z",
}


def lamp_http(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/realtime"):
        return json_response({"error": {"code": "realtime_connection_live", "message": "Live"}}, 409)
    if path.endswith("/connect-tokens"):
        return json_response(GRANT, 201)
    return rest()(request)


class FakeLamps:
    def __init__(self, online: set[str]) -> None:
        self.online = online
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.sent: list[tuple[str, str, Any]] = []

    def is_online(self, device_id: str) -> bool:
        return device_id in self.online

    async def request(self, device_id: str, command: str, args: dict[str, Any]) -> Any:
        self.requests.append((device_id, command, args))
        return {"on": True, "brightness": args.get("brightness", 100), "colorTemp": 4000}

    def send(self, device_id: str, topic: str, payload: Any) -> None:
        self.sent.append((device_id, topic, payload))


TEMPLATES = {"standard": "template_standard", "pro": "template_pro"}


async def bound_store() -> MemoryStore:
    store = MemoryStore()
    await store.bind("lamp-1", LampBinding(user_id="user_1", session_id="sess_1"))
    await store.save_cursor("lamp-1", "7")
    return store


def tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "jsonrpc": "2.0",
        "method": "tool.call",
        "params": {
            "callId": call_id,
            "sessionId": "sess_1",
            "userId": "user_1",
            "turnId": "turn_1",
            "name": name,
            "arguments": args,
            "claimBy": (now + timedelta(seconds=5)).isoformat(),
            "deadlineAt": (now + timedelta(seconds=60)).isoformat(),
        },
    }


async def lamp_backend(harness: Harness, core: FakeCore, device: FakeLamps, store: MemoryStore | None = None) -> LampBackend:
    backend = create_lamp_backend(
        base_url=core.base_url,
        organization_api_key=API_KEY,
        templates=TEMPLATES,
        device=device,
        store=store or await bound_store(),
        http_client=harness.http_client(lamp_http),
    )
    harness.closers.insert(0, backend.close)
    return backend


async def test_an_online_lamps_session_declares_the_lamps_tools_and_forwards_their_calls_to_the_lamp(
    harness: Harness,
) -> None:
    core = await harness.core(
        respond=lambda message, peer, index: (
            {"result": {"claimed": True}}
            if message["method"] == "tool.claim"
            else {"result": {"accepted": True}}
            if message["method"] == "tool.result"
            else None
        )
    )
    device = FakeLamps({"lamp-1"})
    backend = await lamp_backend(harness, core, device)
    await backend.on_device_online("lamp-1")
    peer = core.peers[0]
    attach = by_method(peer, "session.attach")[0]["params"]
    assert attach["cursor"] == "7"
    assert attach["tools"] == LAMP_TOOL_DECLARATIONS
    peer.send(tool_call("call_1", "set_light", {"brightness": 40}))
    await until(lambda: len(by_method(peer, "tool.result")) == 1)
    assert device.requests == [("lamp-1", "set_light", {"brightness": 40})]
    assert by_method(peer, "tool.result")[0]["params"] == {
        "callId": "call_1",
        "output": "The lamp is on at 40% and 4000 K",
    }
    # The lamp drops off the device channel: its tools fail at once.
    device.online.clear()
    peer.send(tool_call("call_2", "get_status", {}))
    await until(lambda: len(by_method(peer, "tool.result")) == 2)
    assert by_method(peer, "tool.result")[1]["params"] == {
        "callId": "call_2",
        "error": {"message": "The lamp is offline"},
    }
    await backend.on_device_offline("lamp-1")
    assert len(by_method(peer, "session.detach")) == 1


async def test_typing_while_the_lamp_is_offline_uses_a_temporary_connection_talking_hands_the_lamp_a_voice_grant(
    harness: Harness,
) -> None:
    def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
        if message["method"] != "session.send":
            return None

        async def finish() -> None:
            await asyncio.sleep(0.01)
            peer.send(finished("20"))
            peer.send(idle("21"))

        tasks.append(asyncio.ensure_future(finish()))
        return {"result": {"interaction": receipt(), "replayed": False}}

    tasks: list[asyncio.Task[None]] = []
    core = await harness.core(respond=respond)
    device = FakeLamps(set())
    backend = await lamp_backend(harness, core, device)
    assert await backend.on_app_message("lamp-1", "Too bright", client_event_id="msg_1") == "Dimmed to 40%."
    peer = core.peers[0]
    assert [m["method"] for m in peer.log] == ["session.attach", "session.send", "session.detach"]
    with pytest.raises(LookupError, match="not online"):
        await backend.on_device_wants_to_talk("lamp-1")
    await backend.on_device_online("lamp-1")
    assert await backend.on_device_wants_to_talk("lamp-1") == GRANT
    assert device.sent == [("lamp-1", "voice_grant", GRANT)]
    await asyncio.gather(*tasks)


async def test_a_lamp_that_another_process_took_over_is_no_longer_online_here(harness: Harness) -> None:
    core = await harness.core()
    backend = await lamp_backend(harness, core, FakeLamps({"lamp-1"}))
    await backend.on_device_online("lamp-1")
    core.peers[0].send(
        {"jsonrpc": "2.0", "method": "session.detached", "params": {"sessionId": "sess_1", "reason": "superseded"}}
    )
    await asyncio.sleep(0.05)
    with pytest.raises(LookupError, match="not online"):
        await backend.on_device_wants_to_talk("lamp-1")
    with pytest.raises(LookupError, match="not bound"):
        await backend.on_device_online("lamp-unbound")


async def test_a_lamps_session_starts_on_its_models_template_and_switches_template_when_the_model_changes(
    harness: Harness,
) -> None:
    requests: list[tuple[str, str | None, Any]] = []

    def http(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        requests.append((path, request.headers.get("idempotency-key"), body))
        if path.endswith("/users"):
            return json_response({"user": {"userId": "user_1", "externalId": body["externalId"]}}, 201)
        if path.endswith("/users/user_1/sessions"):
            session = {"sessionId": "sess_1", "template": {"templateId": body["agentTemplateId"]}}
            return httpx.Response(201, json={"session": session}, headers={"etag": '"session-v1"'})
        if path.endswith("/sessions/sess_1/template-switches"):
            switch = {
                "commandId": "cmd_1",
                "sessionId": "sess_1",
                "agentTemplateId": body["agentTemplateId"],
                "configurationEpoch": 1,
                "status": "pending",
                "handoffTurnId": "turn_handoff",
                "createdAt": "2026-11-13T00:00:00.000Z",
            }
            return json_response({"receipt": switch}, 202)
        return httpx.Response(404)

    core = await harness.core()
    store = MemoryStore()
    backend = create_lamp_backend(
        base_url=core.base_url,
        organization_api_key=API_KEY,
        templates=TEMPLATES,
        device=FakeLamps(set()),
        store=store,
        http_client=harness.http_client(http),
    )
    harness.closers.insert(0, backend.close)

    with pytest.raises(ValueError, match="Unknown lamp model mini"):
        await backend.bind_device("app-user-1", "lamp-1", "mini")
    assert requests == []
    assert await backend.bind_device("app-user-1", "lamp-1", "standard") == "sess_1"
    assert await store.binding("lamp-1") == LampBinding(user_id="user_1", session_id="sess_1")
    created = next(r for r in requests if r[0].endswith("/sessions"))
    assert created[1] == "lamp.lamp-1"
    assert created[2]["agentTemplateId"] == "template_standard"

    with pytest.raises(ValueError, match="Unknown lamp model mini"):
        await backend.change_model("lamp-1", "mini")
    switched = await backend.change_model("lamp-1", "pro", change_id="upgrade-1")
    assert switched["status"] == "pending"
    assert switched["handoffTurnId"] == "turn_handoff"
    path, key, body = requests[-1]
    assert path.endswith("/sessions/sess_1/template-switches")
    assert key == "lamp.lamp-1.upgrade-1"
    assert body == {"agentTemplateId": "template_pro"}
    with pytest.raises(LookupError, match="not bound"):
        await backend.change_model("lamp-unbound", "pro")
