"""Server-sent event framing (``sse.ts`` in the TypeScript SDK)."""

from __future__ import annotations

import codecs
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass

_LINE_END = re.compile(r"[\r\n]")


@dataclass(frozen=True)
class SseFrame:
    id: str
    event: str
    data: str


async def decode_sse(
    chunks: AsyncIterable[bytes],
    on_bytes: Callable[[], None] | None = None,
) -> AsyncIterator[SseFrame]:
    """Decode data frames only. A frame is complete only at its blank line.

    The bytes must be strict UTF-8 (a leading byte order mark is dropped).
    ``on_bytes`` runs for every chunk read, including comment-only keep-alives.
    """
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    buffer = ""
    last_id = ""
    event = ""
    data: list[str] = []
    iterator = chunks.__aiter__()
    try:
        while True:
            try:
                chunk = await iterator.__anext__()
                done = False
            except StopAsyncIteration:
                chunk = b""
                done = True
            if not done and on_bytes is not None:
                on_bytes()
            buffer += decoder.decode(chunk, final=done)
            start = 0
            while (match := _LINE_END.search(buffer, start)) is not None:
                end = match.start()
                if buffer[end] == "\r" and end == len(buffer) - 1 and not done:
                    break
                line = buffer[start:end]
                start = end + (2 if buffer.startswith("\r\n", end) else 1)
                if line == "":
                    if data:
                        yield SseFrame(last_id, event, "\n".join(data))
                    event = ""
                    data = []
                    continue
                field, separator, value = line.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    data.append(value)
                elif field == "event":
                    event = value
                elif field == "id" and "\0" not in value:
                    last_id = value
            buffer = buffer[start:]
            if done:
                return
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass
