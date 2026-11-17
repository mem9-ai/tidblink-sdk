import asyncio
import email.policy
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import aclosing
from email import message_from_bytes
from pathlib import Path

import httpx
import pytest

from audio_support import event, frame, local_server
from support import ORGANIZATION_API_KEY, serve
from tidblink_server import (
    OutcomeUnknownError,
    TiDBLinkApiError,
    TurnFailedError,
    TurnInterruptedError,
)
from tidblink_server._sse import SseFrame, decode_sse
from tidblink_server.audio import AudioTurnResult, AudioTurnStoppedError

ROOT = Path(__file__).resolve().parent.parent
AUDIO = bytes([1, 2, 3, 4])
INPUT = {"filename": "recording.wav", "content_type": "audio/wav"}


def sse(body: str | bytes, status: int = 201) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "text/event-stream"},
        content=body.encode() if isinstance(body, str) else body,
    )


def session_at(handler):
    org, service = serve(handler)
    return org.user("user_1").session("session-audio"), service


def form_parts(content_type: str, body: bytes):
    message = message_from_bytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body, policy=email.policy.HTTP
    )
    return {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}


async def collect(iterator: AsyncIterator) -> list:
    return [item async for item in iterator]


async def test_prerecorded_audio_sends_multipart_bytes_and_json_billing_metadata_and_waits_for_its_own_terminal_result() -> None:
    def handler(request: httpx.Request, service) -> httpx.Response:
        call = service.calls[-1]
        assert call.method == "POST"
        assert call.path == "/api/v1/org/users/user_1/sessions/session-audio/turns/audio"
        assert call.headers["authorization"] == f"Bearer {ORGANIZATION_API_KEY}"
        assert "idempotency-key" not in call.headers
        assert call.headers["accept"] == "text/event-stream"
        assert call.headers["content-length"] == str(len(call.body))
        parts = form_parts(call.headers["content-type"], call.body)
        assert parts["file"].get_filename() == "recording.wav"
        assert parts["file"].get_content_type() == "audio/wav"
        assert parts["file"].get_payload(decode=True) == AUDIO
        assert b'name="billingTag"\r\nContent-Type: application/json\r\n\r\nnull\r\n' in call.body
        return sse(
            frame(event("turn_started", {}))
            + frame(event("assistant_draft", {"text": "provisional"}, 1))
            + frame(event("assistant_message", {"messageId": "answer-1", "text": "Final audio answer"}, 2))
            + frame(event("turn_finished", {"status": "succeeded"}, 3))
        )

    session, _ = session_at(handler)
    assert await session.audio(AUDIO, **INPUT, billing_tag=None) == AudioTurnResult(
        type="answer",
        session_id="session-audio",
        turn_id="turn-audio",
        message_id="answer-1",
        text="Final audio answer",
    )


async def test_audio_sends_the_callers_buffer_without_copying_it_and_a_billing_tag_as_json() -> None:
    received: list[bytes] = []

    def handler(request: httpx.Request, service) -> httpx.Response:
        received.append(service.calls[-1].body)
        return sse(
            frame(event("assistant_message", {"messageId": "m1", "text": "ok", "clarificationItem": {
                "prompt": "Which?", "selectionMode": "single", "answerChoices": ["a", "b"]}}))
            + frame(event("turn_finished", {"status": "succeeded"}, 1))
        )

    session, _ = session_at(handler)
    data = bytearray(b"\x00\xff" * 70_000)
    result = await session.audio(
        memoryview(data), filename='a"b\r\n.wav', billing_tag={"key": "team", "value": "R&D"}
    )
    assert result.type == "clarification"
    assert result.clarification == {"prompt": "Which?", "selectionMode": "single", "answerChoices": ["a", "b"]}
    body = received[0]
    assert bytes(data) in body
    assert b'filename="a%22b%0D%0A.wav"\r\nContent-Type: application/octet-stream\r\n' in body
    assert b'\r\n\r\n{"key":"team","value":"R&D"}\r\n' in body
    # Leaving out billing_tag inherits the Session's default: no part at all.
    await session.audio(AUDIO, filename="x.wav")
    assert b"billingTag" not in received[1]


async def test_audio_progress_preserves_fragmented_sse_envelopes_and_complete_draft_snapshots() -> None:
    events = [
        event("turn_started", {}),
        event("assistant_draft", {"text": "第一版"}, 1),
        event("assistant_draft", {"text": "更正"}, 2),
        event("assistant_draft", {"text": ""}, 3),
        event("progress", {"text": "Working"}, 4),
        event("assistant_message", {"messageId": "m1", "text": "Final"}, 5),
        event("turn_finished", {"status": "succeeded"}, 6),
    ]
    data = (": heartbeat\r\n\r\n" + "".join(frame(e) for e in events)).encode()

    async def pieces() -> AsyncIterator[bytes]:
        for start in range(0, len(data), 3):
            yield data[start : start + 3]

    def handler(request, service):
        return httpx.Response(201, headers={"content-type": "text/event-stream"}, content=pieces())

    session, _ = session_at(handler)
    assert await collect(session.stream_audio(AUDIO, **INPUT)) == events


async def test_audio_turns_deliver_the_client_tool_calls_they_publish_and_refuse_malformed_ones() -> None:
    calls = [
        {"callId": "call_1", "name": "set_light", "arguments": {"on": True}, "deadlineAt": "2099-01-01T00:00:00.000Z"},
        {
            "callId": "call_2",
            "name": "get_status",
            "arguments": {},
            "deadlineAt": "2099-01-01T00:00:00.000Z",
            "claimBy": "2099-01-01T00:00:00.000Z",
        },
    ]
    events = [
        event("turn_started", {}),
        event("client_tool_calls", {"calls": calls}, 1),
        event("assistant_message", {"messageId": "m1", "text": "Done"}, 2),
        event("turn_finished", {"status": "succeeded"}, 3),
    ]
    body = "".join(frame(e) for e in events)
    session, _ = session_at(lambda request, service: sse(body))
    assert await collect(session.stream_audio(AUDIO, **INPUT)) == events
    assert (await session.audio(AUDIO, **INPUT)).text == "Done"

    malformed, _ = session_at(
        lambda request, service: sse(
            frame(event("turn_started", {}))
            + frame(event("client_tool_calls", {"calls": [{"callId": "call_1"}]}, 1))
        )
    )
    with pytest.raises(OutcomeUnknownError) as caught:
        await collect(malformed.stream_audio(AUDIO, **INPUT))
    assert "invalid Client Tool calls" in str(caught.value.cause)


@pytest.mark.parametrize("status", ["failed", "interrupted"])
async def test_audio_final_failure_and_interruption_cannot_promote_a_provisional_or_earlier_message_to_success(
    status: str,
) -> None:
    session, _ = session_at(
        lambda request, service: sse(
            frame(event("turn_started", {}))
            + frame(event("assistant_draft", {"text": "unfinished"}, 1))
            + frame(event("turn_error", {"code": "model_provider_failed", "message": "Provider failed"}, 2))
            + frame(event("turn_finished", {"status": status}, 3))
        )
    )
    if status == "failed":
        with pytest.raises(TurnFailedError) as failed:
            await session.audio(AUDIO, **INPUT)
        assert failed.value.code == "model_provider_failed"
        assert failed.value.session_id == "session-audio"
    else:
        with pytest.raises(TurnInterruptedError):
            await session.audio(AUDIO, **INPUT)


@pytest.mark.parametrize(
    "suffix",
    [
        frame(event("assistant_message", {"messageId": "m1", "text": "Not terminal"}, 1)),
        frame(event("turn_finished", {"status": "succeeded"}, 1, "foreign-turn")),
        frame(event("turn_finished", {"status": "succeeded"}, 1)),
        "event: turn_finished\ndata: {",
    ],
)
async def test_audio_eof_mismatched_execution_and_truncated_frames_retain_uncertainty_without_resubmitting(
    suffix: str,
) -> None:
    session, service = session_at(lambda request, service: sse(frame(event("turn_started", {})) + suffix))
    with pytest.raises(AudioTurnStoppedError) as caught:
        await session.audio(AUDIO, **INPUT)
    assert isinstance(caught.value, OutcomeUnknownError)
    assert caught.value.turn_id == "turn-audio"
    assert caught.value.session_id == "session-audio"
    assert caught.value.input_id is None
    assert len(service.calls) == 1


async def test_audio_refuses_invalid_utf8_and_a_response_without_an_event_stream() -> None:
    session, _ = session_at(lambda request, service: sse(b"event: turn_started\ndata: \xff\n\n"))
    with pytest.raises(AudioTurnStoppedError) as invalid:
        await collect(session.stream_audio(AUDIO, **INPUT))
    assert isinstance(invalid.value.cause, UnicodeDecodeError)
    plain, _ = session_at(lambda request, service: httpx.Response(200, json={}, headers={"x-request-id": "audio-1"}))
    with pytest.raises(AudioTurnStoppedError) as missing:
        await plain.audio(AUDIO, **INPUT)
    assert "omitted its event stream" in str(missing.value.cause)
    assert missing.value.request_id == "audio-1"


class OpenStream(httpx.AsyncByteStream):
    """An event stream that sends ``turn_started`` and then stays open until closed."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield frame(event("turn_started", {})).encode()
        await self.closed.wait()

    async def aclose(self) -> None:
        self.closed.set()


async def test_leaving_audio_consumption_early_closes_its_owning_response() -> None:
    stream = OpenStream()
    session, _ = session_at(
        lambda request, service: httpx.Response(201, headers={"content-type": "text/event-stream"}, stream=stream)
    )
    events = session.stream_audio(AUDIO, **INPUT)
    assert (await anext(events))["event"] == "turn_started"
    await events.aclose()
    await asyncio.wait_for(stream.closed.wait(), 1)


async def test_cancelling_audio_consumption_propagates_and_closes_its_owning_response() -> None:
    stream = OpenStream()
    session, _ = session_at(
        lambda request, service: httpx.Response(201, headers={"content-type": "text/event-stream"}, stream=stream)
    )
    started = asyncio.Event()

    async def consume() -> None:
        async with aclosing(session.stream_audio(AUDIO, **INPUT)) as events:
            async for _ in events:
                started.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(stream.closed.wait(), 1)


async def test_audio_pre_stream_refusal_keeps_the_actual_service_error_and_local_invalid_input_never_dispatches() -> None:
    session, service = session_at(
        lambda request, service: httpx.Response(
            429,
            headers={"x-request-id": "audio-budget"},
            json={"error": {"code": "llm_spend_limit_exhausted", "message": "Limit exhausted"}},
        )
    )
    with pytest.raises(TiDBLinkApiError) as refused:
        await session.audio(AUDIO, **INPUT)
    assert refused.value.code == "llm_spend_limit_exhausted"
    assert refused.value.request_id == "audio-budget"
    with pytest.raises(ValueError, match="prerecorded"):
        await session.audio(b"", **INPUT)
    with pytest.raises(ValueError, match="prerecorded"):
        await session.audio(bytes(25 * 1024 * 1024 + 1), **INPUT)
    with pytest.raises(TypeError, match="client_event_id"):
        await session.audio(AUDIO, **INPUT, client_event_id="not-supported")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await session.audio("not bytes", **INPUT)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="filename"):
        await session.audio(AUDIO, filename=" ")
    with pytest.raises(ValueError, match="filename"):
        await session.audio(AUDIO, filename="a" * 256)
    with pytest.raises(ValueError, match="content_type"):
        await session.audio(AUDIO, filename="a.wav", content_type="audio/wav; rate=16000")
    with pytest.raises(ValueError, match="request_id"):
        await session.audio(AUDIO, **INPUT, request_id="bad id")
    assert len(service.calls) == 1


async def test_decode_sse_frames_cr_line_ends_ids_and_a_leading_byte_order_mark() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for piece in [b"\xef\xbb\xbfid: 7\r", b"\ndata:a\r", b"data: b\r\r", b"id: x\x00y\nevent: e\ndata\n\n", b"data: tail"]:
            yield piece

    seen = 0

    def on_bytes() -> None:
        nonlocal seen
        seen += 1

    assert await collect(decode_sse(chunks(), on_bytes)) == [
        SseFrame("7", "", "a\nb"),
        SseFrame("7", "e", ""),
    ]
    assert seen == 5


# --- Example ----------------------------------------------------------------


def _audio_service(requests: list):
    def handler(method: str, path: str, headers: dict[str, str], body: bytes):
        requests.append((method, path, headers, body))
        return (
            201,
            {"content-type": "text/event-stream"},
            (
                frame(event("turn_started", {}))
                + frame(event("assistant_message", {"messageId": "m1", "text": "Example answer"}, 1))
                + frame(event("turn_finished", {"status": "succeeded"}, 2))
            ).encode(),
        )

    return handler


async def test_submit_audio_example_workflow_returns_the_final_answer() -> None:
    from examples.submit_audio import submit_audio_workflow

    requests: list = []
    with local_server(_audio_service(requests)) as base_url:
        result = await submit_audio_workflow(
            base_url=base_url,
            organization_api_key=ORGANIZATION_API_KEY,
            user_id="user_1",
            session_id="session-audio",
            data=AUDIO,
            filename="recording.wav",
            content_type="audio/wav",
            billing_tag={"key": "team", "value": "audio"},
        )
    assert result.text == "Example answer"
    method, path, headers, body = requests[0]
    assert (method, path) == ("POST", "/api/v1/org/users/user_1/sessions/session-audio/turns/audio")
    assert headers["authorization"] == f"Bearer {ORGANIZATION_API_KEY}"
    assert b'{"key":"team","value":"audio"}' in body


async def test_submit_audio_example_main_reads_the_environment(tmp_path: Path) -> None:
    recording = tmp_path / "clip.wav"
    recording.write_bytes(AUDIO)
    requests: list = []
    with local_server(_audio_service(requests)) as base_url:
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "AGENT_SERVICE_URL": base_url,
            "ORGANIZATION_API_KEY": ORGANIZATION_API_KEY,
            "END_USER_ID": "user_1",
            "SESSION_ID": "session-audio",
            "AUDIO_FILE_PATH": str(recording),
            "AUDIO_CONTENT_TYPE": "audio/wav",
        }
        done = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(ROOT / "examples" / "submit_audio.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "Example answer"
    assert b'filename="clip.wav"\r\nContent-Type: audio/wav' in requests[0][3]
    missing = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(ROOT / "examples" / "submit_audio.py")],
        env={**env, "AUDIO_FILE_PATH": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert missing.returncode == 1
    assert "AUDIO_FILE_PATH" in missing.stderr
