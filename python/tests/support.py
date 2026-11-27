"""A simulated TiDB Link HTTP service for tests, served through ``httpx.MockTransport``."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

from tidblink_server import OrganizationClient

ORGANIZATION_API_KEY = "ti_org_" + "key_" + "x" * 43
BASE_URL = "http://tidblink.test"


@dataclass
class Call:
    method: str
    url: httpx.URL
    headers: httpx.Headers
    body: bytes

    @property
    def path(self) -> str:
        """The request path with its query, percent-decoded like Node's ``request.url``."""
        return self.url.raw_path.decode()

    @property
    def pathname(self) -> str:
        return unquote(self.url.path)

    @property
    def query(self) -> dict[str, str]:
        return dict(self.url.params)

    def json(self) -> Any:
        return json.loads(self.body)


Handler = Callable[[httpx.Request, "Service"], httpx.Response | Awaitable[httpx.Response]]


@dataclass
class Service:
    handler: Handler
    calls: list[Call] = field(default_factory=list)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.calls.append(Call(request.method, request.url, request.headers, body))
        result = self.handler(request, self)
        if inspect.isawaitable(result):
            result = await result
        return result


def json_response(value: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=value, headers=headers)


def error_response(status: int, code: str, message: str = "Refused", **extra: Any) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message, **extra}})


def serve(handler: Handler, **client_options: Any) -> tuple[OrganizationClient, Service]:
    """An OrganizationClient whose HTTP requests go to ``handler``."""
    service = Service(handler)
    client = OrganizationClient(
        base_url=BASE_URL,
        api_key=ORGANIZATION_API_KEY,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(service)),
        **client_options,
    )
    return client, service


def session_record(**overrides: Any) -> dict[str, Any]:
    return {
        "organizationId": "org_1",
        "sessionId": "session_1",
        "name": None,
        "autoTitle": None,
        "ownerUserId": "user_1",
        "startedByUserId": "user_1",
        "createdByType": "user",
        "readOnly": False,
        "sourceSchedulerId": None,
        "sourceSchedulerFireId": None,
        "billingTag": {"key": "customer", "value": "acme"},
        "model": "gpt-5.6-terra",
        "modelRevision": 1,
        "realtimeModel": None,
        "realtimeModelRevision": 1,
        "realtimeModelEtag": '"session-realtime-model:session_1:1"',
        "realtimeLanguage": None,
        "template": {"templateId": "template_1", "contentDigest": "a" * 64},
        "realtime": None,
        "configurationEpoch": 1,
        "switching": False,
        "modelPolicyStatus": "allowed",
        "status": "active",
        "activeTurnId": None,
        "createdAt": "2026-08-25T00:00:00.000Z",
        "updatedAt": "2026-08-25T00:00:00.000Z",
        "deletedAt": None,
        **overrides,
    }


def interaction(**overrides: Any) -> dict[str, Any]:
    return {
        "inputId": "input_1",
        "sequence": 1,
        "kind": "message",
        "mode": "started",
        "turnId": "turn_1",
        "message": {"id": "user_1", "role": "user", "content": "Hello", "createdAt": "2026-10-28T00:00:00Z"},
        "createdAt": "2026-10-28T00:00:00Z",
        "appliedAt": None,
        "rejection": None,
        "result": {"status": "running", "completedAt": None},
        **overrides,
    }
