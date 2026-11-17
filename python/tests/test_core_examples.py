"""The create-session, send-message, control-turn and prepare-realtime-connection examples,
covered as the TypeScript tests cover them (service cases, packed-consumer and CLI runs)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from core_support import KEY, LocalServer, Received
from examples.control_turn import control_turn_workflow
from examples.prepare_realtime_connection import prepare_realtime_connection
from examples.send_message import send_message_workflow

ROOT = Path(__file__).resolve().parents[1]
USER_PATH = "/api/v1/org/users/user_1"
SESSION_PATH = f"{USER_PATH}/sessions/session_1"
COMPLETED_AT = "2026-10-28T00:00:01Z"
TOKEN = "rct_" + "p" * 43


def run_example(name: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    clean = {key: value for key, value in os.environ.items() if key not in _EXAMPLE_ENV}
    return subprocess.run(
        [sys.executable, str(ROOT / "examples" / name)],
        env={**clean, "PYTHONPATH": str(ROOT / "src"), **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


_EXAMPLE_ENV = {
    "AGENT_SERVICE_URL",
    "ORGANIZATION_API_KEY",
    "END_USER_ID",
    "TEMPLATE_ID",
    "SESSION_CREATION_KEY",
    "MODEL_ID",
    "ACKNOWLEDGE_SESSION_RECALL",
    "SESSION_ID",
    "CLIENT_EVENT_ID",
    "MESSAGE_TEXT",
    "TURN_ID",
    "TURN_ACTION",
    "REALTIME_MODEL_ID",
}


def interaction(input_id: str, kind: str = "message", **overrides: Any) -> dict[str, Any]:
    return {
        "inputId": input_id,
        "sequence": 1,
        "kind": kind,
        "mode": {"cancel": "cancelled"}.get(kind, "started"),
        "turnId": "turn_1",
        "message": {"id": "user_1", "role": "user", "content": "Hello", "createdAt": "2026-10-28T00:00:00Z"}
        if kind == "message"
        else None,
        "createdAt": "2026-10-28T00:00:00Z",
        "appliedAt": "2026-10-28T00:00:00Z" if kind == "message" else None,
        "rejection": None,
        "result": {"status": "succeeded", "completedAt": COMPLETED_AT},
        **overrides,
    }


def saved_turn(content: str) -> dict[str, Any]:
    return {
        "id": "turn_1",
        "sessionId": "session_1",
        "status": "succeeded",
        "billingTag": None,
        "operations": [],
        "assistantMessage": {
            "id": "assistant_1",
            "role": "assistant",
            "content": content,
            "sessionId": "session_1",
            "turnId": "turn_1",
            "createdAt": COMPLETED_AT,
        },
        "createdAt": "2026-10-28T00:00:00Z",
        "completedAt": COMPLETED_AT,
    }


# --- create_session ---------------------------------------------------------------


def creation_service(received: Received):
    if received.method == "GET" and "/agent-templates" in received.path:
        return 200, {}, {"templates": [], "hasMore": False, "nextCursor": None}
    if received.method == "GET":
        return 200, {}, {"effectiveDefaultModel": "model-a", "models": []}
    return 201, {"etag": '"model-1"'}, {"session": {"sessionId": "session_1", "model": "model-a"}}


def test_the_create_session_example_runs_from_its_environment_and_is_recoverable() -> None:
    with LocalServer(creation_service) as server:
        env = {
            "AGENT_SERVICE_URL": server.base_url,
            "ORGANIZATION_API_KEY": KEY,
            "END_USER_ID": "user_1",
            "TEMPLATE_ID": "template_1",
            "SESSION_CREATION_KEY": "example-cli",
            "ACKNOWLEDGE_SESSION_RECALL": "true",
        }
        first = run_example("create_session.py", env)
        second = run_example("create_session.py", env)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {"sessionId": "session_1", "model": "model-a"}
    assert second.stdout == first.stdout
    creations = [(r.path, r.headers["idempotency-key"], r.json()) for r in server.requests if r.method == "POST"]
    creation = (
        f"{USER_PATH}/sessions",
        "example-cli",
        {"agentTemplateId": "template_1", "acknowledgeSessionRecall": True},
    )
    assert creations == [creation, creation]
    assert KEY not in first.stdout + first.stderr


def test_the_create_session_example_names_its_required_environment() -> None:
    result = run_example("create_session.py", {"AGENT_SERVICE_URL": "http://127.0.0.1:1"})
    assert result.returncode == 1
    assert "Set AGENT_SERVICE_URL, ORGANIZATION_API_KEY, END_USER_ID, TEMPLATE_ID and SESSION_CREATION_KEY" in (
        result.stderr
    )


# --- send_message -----------------------------------------------------------------


def message_service(received: Received):
    if received.method == "POST":
        body = received.json()
        return 202, {}, {"interaction": interaction(body["clientEventId"], result={"status": "running", "completedAt": None})}
    if "/turns/" in received.path:
        return 200, {}, {"turn": saved_turn("SDK final answer.")}
    return 200, {}, {"interaction": interaction(received.path.rsplit("/", 1)[1])}


def test_the_send_message_example_returns_the_original_result_and_recovers_it() -> None:
    with LocalServer(message_service) as server:
        options = dict(
            base_url=server.base_url,
            organization_api_key=KEY,
            user_id="user_1",
            session_id="session_1",
            client_event_id="message-1",
            text="SDK_FIXTURE_SUCCESS",
            timeout=15.0,
        )
        result = asyncio.run(send_message_workflow(**options))
        repeated = asyncio.run(send_message_workflow(**options))
    assert result.text == "SDK final answer."
    assert result.interaction["result"]["status"] == "succeeded"
    assert result.interaction["appliedAt"]
    assert repeated == result
    posts = [r for r in server.requests if r.method == "POST"]
    assert [(r.path, r.json()) for r in posts] == [
        (
            f"{SESSION_PATH}/events",
            {"type": "agent.session.input.message", "clientEventId": "message-1", "data": {"text": "SDK_FIXTURE_SUCCESS"}},
        )
    ] * 2


def test_the_send_message_example_prints_the_answer_or_the_failure() -> None:
    with LocalServer(message_service) as server:
        env = {
            "AGENT_SERVICE_URL": server.base_url,
            "ORGANIZATION_API_KEY": KEY,
            "END_USER_ID": "user_1",
            "SESSION_ID": "session_1",
            "CLIENT_EVENT_ID": "message-cli",
            "MESSAGE_TEXT": "Hello",
        }
        answered = run_example("send_message.py", env)
        failed = run_example("send_message.py", {**env, "CLIENT_EVENT_ID": "bad key"})
    assert answered.returncode == 0, answered.stderr
    assert answered.stdout == "SDK final answer.\n"
    assert failed.returncode == 1
    assert "requires a stable idempotency_key" in failed.stderr


# --- control_turn -----------------------------------------------------------------


def control_service(received: Received):
    if received.method == "POST":
        body = received.json()
        if body["type"] == "agent.session.input.cancel":
            return 202, {}, {
                "interaction": interaction(
                    body["clientEventId"], "cancel", result={"status": "running", "completedAt": None}
                )
            }
        return 202, {}, {
            "event": {
                "clientEventId": body["clientEventId"],
                "sessionId": "session_1",
                "turnId": body["data"]["turnId"],
                "disposition": "retrying",
            }
        }
    if "/turns/" in received.path:
        return 200, {}, {"turn": saved_turn("Retried task completed.")}
    return 200, {}, {"interaction": interaction(received.path.rsplit("/", 1)[1], "retry")}


def test_the_control_turn_example_cancels_or_retries_and_waits() -> None:
    with LocalServer(control_service) as server:
        common = dict(
            base_url=server.base_url, organization_api_key=KEY, user_id="user_1", session_id="session_1", turn_id="turn_1"
        )
        accepted = asyncio.run(control_turn_workflow(**common, client_event_id="cancel-1", action="cancel"))
        retried = asyncio.run(control_turn_workflow(**common, client_event_id="retry-1", action="retry"))
        with pytest.raises(ValueError, match="action must be cancel or retry"):
            asyncio.run(control_turn_workflow(**common, client_event_id="x-1", action="pause"))  # type: ignore[arg-type]
    assert accepted["kind"] == "cancel"
    assert accepted["turnId"] == "turn_1"
    assert retried.text == "Retried task completed."
    assert retried.interaction["kind"] == "retry"
    assert retried.input_id == "retry-1"
    assert [(r.method, r.path) for r in server.requests] == [
        ("POST", f"{SESSION_PATH}/events"),
        ("POST", f"{SESSION_PATH}/events"),
        ("GET", f"{SESSION_PATH}/interactions/retry-1"),
        ("GET", f"{SESSION_PATH}/turns/turn_1"),
    ]
    assert [r.json()["type"] for r in server.requests if r.method == "POST"] == [
        "agent.session.input.cancel",
        "agent.session.input.retry",
    ]


def test_the_control_turn_example_prints_the_result_as_json() -> None:
    with LocalServer(control_service) as server:
        env = {
            "AGENT_SERVICE_URL": server.base_url,
            "ORGANIZATION_API_KEY": KEY,
            "END_USER_ID": "user_1",
            "SESSION_ID": "session_1",
            "TURN_ID": "turn_1",
            "CLIENT_EVENT_ID": "retry-cli",
        }
        retried = run_example("control_turn.py", {**env, "TURN_ACTION": "retry"})
        refused = run_example("control_turn.py", {**env, "TURN_ACTION": "pause"})
    assert retried.returncode == 0, retried.stderr
    printed = json.loads(retried.stdout)
    assert printed["text"] == "Retried task completed."
    assert printed["input_id"] == "retry-cli"
    assert refused.returncode == 1
    assert refused.stderr.strip() == "action must be cancel or retry"


# --- prepare_realtime_connection --------------------------------------------------


def realtime_service(received: Received):
    if received.path == f"{USER_PATH}/realtime-models":
        return 200, {}, {
            "defaultModel": "voice-a",
            "models": [
                {
                    "id": "voice-a",
                    "label": "Voice A",
                    "available": True,
                    "isDefault": True,
                    "languages": ["en-US"],
                    "endpointingModes": ["client"],
                    "audio": {"input": [], "output": []},
                }
            ],
        }
    selection = {
        "realtimeModel": "voice-a",
        "realtimeModelRevision": 1,
        "realtimeModelEtag": "realtime-v1",
        "realtimeVoice": {"language": "en-US"},
    }
    if received.path == SESSION_PATH:
        return 200, {}, {"session": selection}
    if received.path == f"{SESSION_PATH}/realtime-model":
        return 200, {}, {
            "session": {
                **selection,
                "realtimeModel": received.json()["realtimeModel"],
                "realtimeModelRevision": 2,
                "realtimeModelEtag": "realtime-v2",
            }
        }
    if received.path == f"{SESSION_PATH}/realtime":
        return 201, {}, {
            "realtimeConnection": {
                "object": "realtime_connection",
                "id": "rtc_1",
                "sessionId": "session_1",
                "provider": {"name": "mock", "region": "test", "model": "voice-a"},
                "status": "connecting",
                "audio": {
                    "input": {"encoding": "pcm16", "sampleRate": 16000, "channels": 1},
                    "output": {"encoding": "pcm16", "sampleRate": 24000, "channels": 1},
                },
                "activeTaskTurnId": None,
                "usage": {"inputAudioSec": 0, "outputAudioSec": 0, "thoughtTokens": 0, "textTokens": 0},
                "metadata": {},
                "createdAt": "2026-10-27T00:00:00.000Z",
                "endedAt": None,
            },
            "websocketUrl": "wss://voice.example/ws",
            "connectToken": TOKEN,
            "connectTokenExpiresAt": "2026-10-27T00:01:00.000Z",
        }
    return 404, {}, {"error": {"code": "not_found", "message": "Not found"}}


def test_the_prepare_realtime_connection_example_returns_the_one_time_grant() -> None:
    with LocalServer(realtime_service) as server:
        prepared = asyncio.run(
            prepare_realtime_connection(
                base_url=server.base_url, organization_api_key=KEY, user_id="user_1", session_id="session_1"
            )
        )
        server.requests.clear()
        selected = asyncio.run(
            prepare_realtime_connection(
                base_url=server.base_url,
                organization_api_key=KEY,
                user_id="user_1",
                session_id="session_1",
                realtime_model_id="voice-b",
                metadata={"terminal": "kiosk-1"},
            )
        )
        reselect = [(r.method, r.path, r.headers.get("if-match")) for r in server.requests]
        open_body = server.requests[-1].json()
    assert prepared["realtimeConnection"]["id"] == "rtc_1"
    assert prepared["connectToken"].startswith("rct_")
    assert prepared["selection"]["model"] == "voice-a"
    assert prepared["catalog"]["defaultModel"] == "voice-a"
    assert selected["selection"] == {"model": "voice-b", "revision": 2, "etag": "realtime-v2", "voice": {"language": "en-US"}}
    assert reselect == [
        ("GET", f"{USER_PATH}/realtime-models", None),
        ("GET", SESSION_PATH, None),
        ("PATCH", f"{SESSION_PATH}/realtime-model", "realtime-v1"),
        ("POST", f"{SESSION_PATH}/realtime", None),
    ]
    assert open_body == {"metadata": {"terminal": "kiosk-1"}}


def test_the_prepare_realtime_connection_example_never_prints_the_connect_token() -> None:
    with LocalServer(realtime_service) as server:
        result = run_example(
            "prepare_realtime_connection.py",
            {
                "AGENT_SERVICE_URL": server.base_url,
                "ORGANIZATION_API_KEY": KEY,
                "END_USER_ID": "user_1",
                "SESSION_ID": "session_1",
                "REALTIME_MODEL_ID": "voice-b",
            },
        )
    assert result.returncode == 0, result.stderr
    assert "rct_" not in result.stdout + result.stderr
    output = json.loads(result.stdout)
    assert output["realtimeConnection"]["sessionId"] == "session_1"
    assert "connectToken" not in output
    assert isinstance(output["connectTokenExpiresAt"], str)
    assert [(r.method, r.path) for r in server.requests] == [
        ("GET", f"{USER_PATH}/realtime-models"),
        ("GET", SESSION_PATH),
        ("PATCH", f"{SESSION_PATH}/realtime-model"),
        ("POST", f"{SESSION_PATH}/realtime"),
    ]
    assert all(r.headers["authorization"] == f"Bearer {KEY}" for r in server.requests)
