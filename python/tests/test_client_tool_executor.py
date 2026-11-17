"""Port of ``typescript/test/client-tool-executor.test.mjs``."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable

from backend_support import FakeCore, Harness, by_method, deadline_in, harness, until  # noqa: F401
from tidblink_server.session import Session
from tidblink_server.session_connection import (
    ClientToolCallContext,
    ClientToolHandler,
    ClientToolResultRefusedError,
)


def session_of(harness: Harness, core: FakeCore, session_id: str = "sess_1") -> Session:
    return harness.client(core).user("user_1").session(session_id)


def call(call_id: str, name: str = "set_light", args: dict[str, Any] | None = None, deadline: float = 60.0) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "tool.call",
        "params": {
            "callId": call_id,
            "sessionId": "sess_1",
            "userId": "user_1",
            "turnId": "turn_1",
            "name": name,
            "arguments": {"on": True} if args is None else args,
            "claimBy": deadline_in(5),
            "deadlineAt": deadline_in(deadline),
        },
    }


def granted(message: dict[str, Any]) -> Any:
    return {"result": {"claimed": True}} if message["method"] == "tool.claim" else None


def accepted(message: dict[str, Any]) -> Any:
    return {"result": {"accepted": True}} if message["method"] == "tool.result" else None


def lamp(runs: list[tuple[dict[str, Any], ClientToolCallContext]], run: Callable[..., Any] | None = None) -> dict[str, ClientToolHandler]:
    async def execute(args: dict[str, Any], context: ClientToolCallContext) -> Any:
        runs.append((args, context))
        return run(args, context) if run else f"Lamp {'on' if args['on'] else 'off'}"

    return {
        "set_light": ClientToolHandler(
            description="Turn the lamp on or off",
            parameters={"type": "object", "properties": {"on": {"type": "boolean"}}},
            run=execute,
        )
    }


async def test_a_call_runs_only_after_its_claim_is_granted_and_its_result_goes_back_to_the_core(
    harness: Harness,
) -> None:
    order: list[str] = []

    def respond(message: dict[str, Any], peer: Any, index: int) -> Any:
        if message["method"] == "tool.claim":
            order.append("claim")
        if message["method"] == "tool.result":
            order.append("result")
        return granted(message) or accepted(message)

    core = await harness.core(respond=respond)
    runs: list[Any] = []

    def run(args: dict[str, Any], context: ClientToolCallContext) -> str:
        order.append("run")
        return f"Lamp {'on' if args['on'] else 'off'}"

    conn = await session_of(harness, core).connect(tools=lamp(runs, run))
    peer = core.peers[0]
    peer.send(call("call_1"))
    await until(lambda: len(by_method(peer, "tool.result")) == 1)
    assert order == ["claim", "run", "result"]
    assert by_method(peer, "tool.claim")[0]["params"] == {"callId": "call_1"}
    assert by_method(peer, "tool.result")[0]["params"] == {"callId": "call_1", "output": "Lamp on"}
    args, context = runs[0]
    assert args == {"on": True}
    assert context.call_id == "call_1"
    assert context.session_id == "sess_1"
    assert context.user_id == "user_1"
    assert context.turn_id == "turn_1"
    assert context.name == "set_light"
    assert isinstance(context.deadline_at, datetime)
    assert context.deadline_at.tzinfo is not None
    await conn.close()


async def test_a_refused_claim_never_runs_duplicates_run_once_and_a_superseded_claim_may_run_after_redelivery(
    harness: Harness,
) -> None:
    claims = 0

    def respond(message: dict[str, Any], peer: Any, index: int) -> Any:
        nonlocal claims
        if message["method"] != "tool.claim":
            return accepted(message)
        claims += 1
        if message["params"]["callId"] == "late":
            return {"result": {"claimed": False, "reason": "claim_window_closed"}}
        if message["params"]["callId"] == "moved" and claims <= 2:
            return {"result": {"claimed": False, "reason": "superseded"}}
        return {"result": {"claimed": True}}

    core = await harness.core(respond=respond)
    runs: list[Any] = []
    await session_of(harness, core).connect(tools=lamp(runs))
    peer = core.peers[0]
    peer.send(call("late"))
    await until(lambda: claims == 1)
    peer.send(call("late"))
    peer.send(call("moved"))
    await until(lambda: claims == 2)
    await asyncio.sleep(0.03)
    assert runs == []
    peer.send(call("moved"))
    await until(lambda: len(by_method(peer, "tool.result")) == 1)
    peer.send(call("moved"))
    await asyncio.sleep(0.03)
    assert claims == 3
    assert [context.call_id for _, context in runs] == ["moved"]


async def test_errors_values_missing_handlers_and_deadlines_become_results_the_model_can_read(
    harness: Harness,
) -> None:
    core = await harness.core(respond=lambda message, peer, index: granted(message) or accepted(message))
    cancelled: list[BaseException] = []

    def fail(args: Any, context: Any) -> Any:
        raise RuntimeError("Lamp offline")

    async def hang(args: Any, context: Any) -> Any:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            cancelled.append(error)
            raise

    tools = {
        "fail": ClientToolHandler("Fails", {}, fail),
        "status": ClientToolHandler("Status", {}, lambda args, context: {"on": True, "brightness": 40}),
        "nothing": ClientToolHandler("Nothing", {}, lambda args, context: None),
        "huge": ClientToolHandler("Huge", {}, lambda args, context: "x" * 65537),
        "hang": ClientToolHandler("Hangs", {}, hang),
    }
    await session_of(harness, core).connect(tools=tools)
    peer = core.peers[0]
    peer.send(call("c_fail", "fail", {}))
    peer.send(call("c_status", "status", {}))
    peer.send(call("c_nothing", "nothing", {}))
    peer.send(call("c_huge", "huge", {}))
    peer.send(call("c_unknown", "unknown_tool", {}))
    peer.send(call("c_hang", "hang", {}, 0.3))
    await until(lambda: len(by_method(peer, "tool.result")) == 6)
    results = {
        m["params"]["callId"]: {k: v for k, v in m["params"].items() if k != "callId"}
        for m in by_method(peer, "tool.result")
    }
    assert results["c_fail"] == {"error": {"message": "Lamp offline"}}
    assert results["c_status"] == {"output": '{"on":true,"brightness":40}'}
    assert results["c_nothing"] == {"output": ""}
    assert "exceeds 65536 characters" in results["c_huge"]["error"]["message"]
    assert "unknown_tool has no handler" in results["c_unknown"]["error"]["message"]
    assert "deadline" in results["c_hang"]["error"]["message"]
    # The deadline cancelled the handler's task.
    await until(lambda: len(cancelled) == 1)


async def test_a_lost_claim_answer_closes_the_call_as_failed_without_running_it(harness: Harness) -> None:
    def respond(message: dict[str, Any], peer: Any, index: int) -> Any:
        if message["method"] == "tool.claim" and index == 0:
            peer.destroy()
            return "silent"
        return accepted(message)

    core = await harness.core(respond=respond)
    runs: list[Any] = []
    await session_of(harness, core).connect(tools=lamp(runs))
    core.peers[0].send(call("call_1"))
    following = await core.connection(1)
    await until(lambda: len(by_method(following, "tool.result")) == 1)
    assert runs == []
    result = by_method(following, "tool.result")[0]["params"]
    assert result["callId"] == "call_1"
    assert "did not execute" in result["error"]["message"]


async def test_a_result_survives_a_lost_connection_and_a_refused_one_reaches_on_error(harness: Harness) -> None:
    results = 0

    def respond(message: dict[str, Any], peer: Any, index: int) -> Any:
        nonlocal results
        if message["method"] != "tool.result":
            return granted(message)
        results += 1
        if results == 1:
            peer.destroy()
            return "silent"
        if message["params"]["callId"] == "settled":
            return {"result": {"accepted": False, "reason": "call_settled"}}
        return {"result": {"accepted": True}}

    core = await harness.core(respond=respond)
    runs: list[Any] = []
    errors: list[BaseException] = []
    await session_of(harness, core).connect(tools=lamp(runs), on_error=errors.append)
    core.peers[0].send(call("call_1"))
    following = await core.connection(1)
    await until(lambda: len(by_method(following, "tool.result")) == 1)
    assert by_method(following, "tool.result")[0]["params"] == {"callId": "call_1", "output": "Lamp on"}
    assert len(runs) == 1
    await until(lambda: len(by_method(following, "session.attach")) == 1)
    following.send(call("settled"))
    await until(lambda: len(errors) == 1)
    assert isinstance(errors[0], ClientToolResultRefusedError)
    assert errors[0].call_id == "settled"
    assert errors[0].reason == "call_settled"


async def test_an_observing_connection_never_claims_calls(harness: Harness) -> None:
    core = await harness.core()
    await session_of(harness, core).connect(observe=True)
    peer = core.peers[0]
    peer.send(call("call_1"))
    await asyncio.sleep(0.05)
    assert by_method(peer, "tool.claim") == []


async def test_organization_client_close_cancels_calls_still_running(harness: Harness) -> None:
    core = await harness.core(respond=lambda message, peer, index: granted(message) or accepted(message))
    started = asyncio.Event()
    cancelled: list[BaseException] = []

    async def hang(args: Any, context: Any) -> Any:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            cancelled.append(error)
            raise

    client = harness.client(core)
    await client.user("user_1").session("sess_1").connect(
        tools={"hang": ClientToolHandler("Hangs", {}, hang)}, on_error=lambda error: None
    )
    core.peers[0].send(call("c_hang", "hang", {}))
    await asyncio.wait_for(started.wait(), 5)
    await client.close()
    assert len(cancelled) == 1
