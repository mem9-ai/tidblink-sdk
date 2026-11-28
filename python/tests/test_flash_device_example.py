"""The flash-device example (``typescript/test/flash-device-example.test.mjs``)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx

from backend_support import (  # noqa: F401
    API_KEY,
    SAVED_TURN,
    Harness,
    Peer,
    by_method,
    deadline_in,
    finished,
    harness,
    idle,
    json_response,
    methods,
    receipt,
    settled,
    until,
)
from examples.flash_device import DownloadingDevice, connect_device_flasher, flash_device_workflow

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples" / "flash_device.py"
REPLY = "Built and flashed the firmware."
SESSION_PATH = "/api/v1/org/users/user_1/sessions/sess_1"
DOWNLOADS = "/api/v1/drive-downloads/ticket."
AT = "2026-11-26T00:00:00.000Z"
FIRMWARE = {
    "path": "builds/firmware.bin",
    "name": "Firmware 1",
    "version": "v1",
    "byteSize": 11,
    "updatedAt": AT,
}


def respond(message: dict[str, Any], peer: Peer, index: int) -> Any:
    return {
        "tool.claim": {"result": {"claimed": True}},
        "tool.result": {"result": {"accepted": True}},
    }.get(message["method"])


def drive_service(drive: dict[str, dict[str, Any]], seen: list[tuple[str, str, str | None, Any]]) -> Any:
    """Mints tickets for the files in ``drive`` and redeems them, recording each request."""

    async def handle(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        path = request.url.raw_path.decode()
        seen.append((request.method, path, request.headers.get("authorization"), json.loads(body) if body else None))
        if request.method == "POST" and path == f"{SESSION_PATH}/files/download-tickets":
            asked = json.loads(body)
            file = drive.get(asked["path"])
            if file is None:
                return json_response({"error": {"code": "drive_file_not_found", "message": "Not found"}}, 404)
            if file["version"] != asked.get("version"):
                return json_response(
                    {"error": {"code": "drive_file_version_conflict", "message": "The file changed after it was announced"}},
                    409,
                )
            return json_response(
                {
                    "url": f"{DOWNLOADS}{quote(asked['path'], safe='')}",
                    "expiresAt": "2099-01-01T00:01:00.000Z",
                    "byteSize": len(file["bytes"]),
                },
                201,
            )
        if request.method == "GET" and path.startswith(DOWNLOADS):
            return httpx.Response(200, content=drive[unquote(path[len(DOWNLOADS) :])]["bytes"])
        # The settled Turn of message msg_1.
        if request.method == "GET" and path == f"{SESSION_PATH}/interactions/msg_1":
            return json_response({"interaction": settled()})
        if request.method == "GET" and path == f"{SESSION_PATH}/turns/turn_1":
            return json_response({"turn": {**SAVED_TURN, "assistantMessage": {**SAVED_TURN["assistantMessage"], "content": REPLY}}})
        return httpx.Response(404)

    return handle


def notified(cursor: str, file: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session.event",
        "params": {
            "sessionId": "sess_1",
            "cursor": cursor,
            "event": {
                "type": "agent.session.turn.item.added",
                "sessionId": "sess_1",
                "turnId": "turn_1",
                "createdAt": AT,
                "data": {
                    "event": "operation_step",
                    "turnId": "turn_1",
                    "sessionId": "sess_1",
                    "seq": int(cursor),
                    "createdAt": AT,
                    "payload": {"operationId": "op_1", "status": "succeeded", "label": "Notified file", "file": file},
                },
            },
        },
    }


def tool_call(call_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "tool.call",
        "params": {
            "callId": call_id,
            "sessionId": "sess_1",
            "userId": "user_1",
            "turnId": "turn_1",
            "name": "flash_device",
            "arguments": args,
            "claimBy": deadline_in(5),
            "deadlineAt": deadline_in(60),
        },
    }


async def test_the_flash_device_example_announces_the_file_and_flashes_the_announced_version_through_a_keyless_ticket(
    harness: Harness,
) -> None:
    drive = {FIRMWARE["path"]: {"version": "v1", "bytes": b"firmware v1"}}
    seen: list[tuple[str, str, str | None, Any]] = []
    service = drive_service(drive, seen)
    core = await harness.core(respond=respond)
    session = harness.client(core, service).user("user_1").session("sess_1")
    announced: list[Any] = []
    flashed: list[bytes] = []
    conn = await connect_device_flasher(
        session,
        DownloadingDevice(flashed.append, http_client=harness.http_client(service)),
        on_file=announced.append,
    )
    peer = core.peers[0]
    assert by_method(peer, "session.attach")[0]["params"]["tools"] == [
        {
            "type": "function",
            "name": "flash_device",
            "description": "Flash a firmware image onto the device. Call notify_file first and pass its path and version.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "version": {"type": "string"}},
                "required": ["path", "version"],
                "additionalProperties": False,
            },
        }
    ]
    peer.send(notified("11", FIRMWARE))
    await until(lambda: len(announced) == 1)
    assert announced == [FIRMWARE]

    peer.send(tool_call("call_1", {"path": FIRMWARE["path"], "version": FIRMWARE["version"]}))
    await until(lambda: len(by_method(peer, "tool.result")) == 1)
    assert by_method(peer, "tool.result")[0]["params"] == {"callId": "call_1", "output": "Flashed 11 bytes"}
    assert flashed == [b"firmware v1"]
    assert seen == [
        (
            "POST",
            f"{SESSION_PATH}/files/download-tickets",
            f"Bearer {API_KEY}",
            {"path": FIRMWARE["path"], "version": "v1"},
        ),
        # The device downloads without the API Key.
        ("GET", f"{DOWNLOADS}{quote(FIRMWARE['path'], safe='')}", None, None),
    ]

    # The Agent changed the file after announcing it: the model learns so.
    drive[FIRMWARE["path"]] = {"version": "v2", "bytes": b"firmware v2"}
    peer.send(tool_call("call_2", {"path": FIRMWARE["path"], "version": FIRMWARE["version"]}))
    await until(lambda: len(by_method(peer, "tool.result")) == 2)
    assert by_method(peer, "tool.result")[1]["params"] == {
        "callId": "call_2",
        "error": {"message": "The file changed after it was announced"},
    }
    assert flashed == [b"firmware v1"]
    await conn.close()


async def test_flash_device_workflow_keeps_the_device_connected_until_the_turn_ends_so_a_flash_device_call_late_in_the_build_reaches_it(
    harness: Harness,
) -> None:
    drive = {FIRMWARE["path"]: {"version": "v1", "bytes": b"firmware v1"}}
    service = drive_service(drive, [])
    tasks: set[asyncio.Task[None]] = set()

    def later(delay: float, *messages: dict[str, Any], peer: Peer) -> None:
        async def run() -> None:
            await asyncio.sleep(delay)
            for message in messages:
                peer.send(message)

        tasks.add(asyncio.ensure_future(run()))

    def turn(message: dict[str, Any], peer: Peer, index: int) -> Any:
        if message["method"] == "session.send":
            # The Agent builds for a while, announces the image, then calls the tool.
            later(
                0.2,
                notified("11", FIRMWARE),
                tool_call("call_1", {"path": FIRMWARE["path"], "version": FIRMWARE["version"]}),
                peer=peer,
            )
            return {"result": {"interaction": receipt(), "replayed": False}}
        if message["method"] == "tool.result":
            later(0.02, finished("12"), idle("13"), peer=peer)
        return respond(message, peer, index)

    core = await harness.core(respond=turn)
    session = harness.client(core, service).user("user_1").session("sess_1")
    announced: list[Any] = []
    flashed: list[bytes] = []
    reply = await flash_device_workflow(
        session,
        DownloadingDevice(flashed.append, http_client=harness.http_client(service)),
        "Build the firmware and flash it",
        client_event_id="msg_1",
        timeout=5,
        on_file=announced.append,
    )
    assert reply.text == REPLY
    assert announced == [FIRMWARE]
    assert flashed == [b"firmware v1"]
    peer = core.peers[0]
    assert by_method(peer, "tool.result")[0]["params"] == {"callId": "call_1", "output": "Flashed 11 bytes"}
    # Connected for the whole Turn, disconnected once it ended.
    assert [m for m in methods(peer) if m != "ping"] == [
        "session.attach",
        "session.send",
        "tool.claim",
        "tool.result",
        "session.detach",
    ]


async def run_script(env: dict[str, str]) -> tuple[int | None, bytes, bytes]:
    names = ("FIRMWARE_PATH", "MESSAGE_TEXT", "TURN_TIMEOUT_SECONDS")
    base = {key: value for key, value in os.environ.items() if key not in names}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPT),
        env={**base, "PYTHONPATH": str(ROOT / "src"), **env},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return process.returncode, stdout, stderr


async def test_the_flash_device_script_waits_turn_timeout_seconds_for_the_turn_then_says_how_to_wait_longer(
    harness: Harness, tmp_path: Path
) -> None:
    def still_building(message: dict[str, Any], peer: Peer, index: int) -> Any:
        if message["method"] == "session.send":
            return {"result": {"interaction": receipt(), "replayed": False}}
        return None

    core = await harness.core(respond=still_building)
    code, stdout, stderr = await run_script(
        {
            "AGENT_SERVICE_URL": core.base_url,
            "ORGANIZATION_API_KEY": API_KEY,
            "END_USER_ID": "user_1",
            "SESSION_ID": "sess_1",
            "MESSAGE_TEXT": "Build the firmware and flash it",
            "CLIENT_EVENT_ID": "msg_1",
            "FIRMWARE_PATH": str(tmp_path / "device.bin"),
            "TURN_TIMEOUT_SECONDS": "1",
        }
    )
    assert code == 1, stderr
    assert stdout == b""
    assert b"did not finish within 1 second" in stderr
    assert b"TURN_TIMEOUT_SECONDS" in stderr
    assert len(by_method(core.peers[0], "session.send")) == 1


async def test_the_flash_device_script_names_a_missing_or_invalid_setting_before_connecting() -> None:
    code, stdout, stderr = await run_script({})
    assert code == 1
    assert stdout == b""
    assert b"Set FIRMWARE_PATH" in stderr
    code, stdout, stderr = await run_script(
        {"FIRMWARE_PATH": "device.bin", "MESSAGE_TEXT": "Build and flash", "TURN_TIMEOUT_SECONDS": "ten minutes"}
    )
    assert code == 1
    assert stdout == b""
    assert b"Set TURN_TIMEOUT_SECONDS" in stderr
