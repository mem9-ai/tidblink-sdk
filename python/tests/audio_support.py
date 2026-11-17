"""Helpers for audio and usage tests: audio events, SSE frames and a real local HTTP server."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def event(name: str, payload: dict[str, Any], seq: int = 0, turn_id: str = "turn-audio") -> dict[str, Any]:
    return {
        "event": name,
        "sessionId": "session-audio",
        "turnId": turn_id,
        "seq": seq,
        "createdAt": "2026-10-28T00:00:00.000Z",
        "payload": payload,
    }


def frame(value: dict[str, Any]) -> str:
    return f"event: {value['event']}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"


#: ``handler(method, path, headers, body)`` returns ``(status, headers, body)``.
LocalHandler = Callable[[str, str, dict[str, str], bytes], tuple[int, dict[str, str], bytes]]


@contextmanager
def local_server(handler: LocalHandler) -> Iterator[str]:
    """A real HTTP server on 127.0.0.1 in a thread; yields its base URL."""

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length)
            status, headers, content = handler(
                self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body
            )
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("content-length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        do_GET = do_POST = _serve

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
