"""Helpers for the core REST tests: handles over the simulated service, and a real local HTTP server."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from support import ORGANIZATION_API_KEY, Handler, Service, serve
from tidblink_server import EndUserClient, Session

KEY = ORGANIZATION_API_KEY


def disconnect(request: httpx.Request | None = None, service: Service | None = None) -> httpx.Response:
    """Lose the response, like a server that destroys the socket."""
    raise httpx.RemoteProtocolError("Server disconnected without sending a response")


def user_for(handler: Handler, user_id: str = "user_1") -> tuple[EndUserClient, Service]:
    client, service = serve(handler)
    return client.user(user_id), service


def session_for(handler: Handler, session_id: str = "session_1") -> tuple[Session, Service]:
    user, service = user_for(handler)
    return user.session(session_id), service


# --- A real HTTP server, for examples that build their own client ----------------


@dataclass
class Received:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


Reply = tuple[int, dict[str, str], Any]
ServerHandler = Callable[[Received], Reply]


@dataclass
class LocalServer:
    """A threaded HTTP/1.1 server; ``handler`` returns ``(status, headers, json_value)``."""

    handler: ServerHandler
    requests: list[Received] = field(default_factory=list)

    def __enter__(self) -> LocalServer:
        owner = self

        class RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def _handle(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                received = Received(
                    self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body
                )
                owner.requests.append(received)
                status, headers, value = owner.handler(received)
                payload = b"" if value is None else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                for name, header in headers.items():
                    self.send_header(name, header)
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"
