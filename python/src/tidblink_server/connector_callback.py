"""Verification of platform callbacks to an HTTP Connector (``connector-callback.ts`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, NoReturn, TypedDict
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .errors import TiDBLinkError

__all__ = [
    "CONNECTOR_CALLBACK_ISSUER",
    "CONNECTOR_CALLBACK_AUDIENCE",
    "CONNECTOR_CALL_CONTEXT_KEY",
    "ConnectorVerificationKey",
    "ConnectorCallbackClaims",
    "ConnectorCallContext",
    "ConnectorAuthError",
    "ConnectorKeySet",
    "verify_connector_callback",
    "read_connector_call_context",
]

CONNECTOR_CALLBACK_ISSUER = "tidb-link/connector-callback/v1"
CONNECTOR_CALLBACK_AUDIENCE = "tidb-link/connector"
#: The ``params._meta`` key of the Call Context on every Connector ``tools/call``.
CONNECTOR_CALL_CONTEXT_KEY = "tidb-link/context"

_MAX_SAFE_INTEGER = 2**53 - 1
_BEARER = re.compile(r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", re.IGNORECASE)


class ConnectorVerificationKey(TypedDict):
    """A public Ed25519 key that signs Connector callback tokens."""

    kty: Literal["OKP"]
    crv: Literal["Ed25519"]
    x: str
    kid: str
    alg: Literal["EdDSA"]
    use: Literal["sig"]


class ConnectorCallbackClaims(TypedDict):
    """The verified claims of a Connector callback token, as the platform signed them."""

    iss: Literal["tidb-link/connector-callback/v1"]
    aud: Literal["tidb-link/connector"]
    organizationId: str
    connectorId: str
    iat: int
    exp: int


@dataclass(frozen=True)
class ConnectorCallContext:
    """Who a Connector tool call is for, written by the platform from the Session's persisted owner.

    Model arguments cannot override it.
    """

    #: Globally unique; deduplicate side effects by it.
    call_id: str
    session_id: str
    organization_id: str
    #: The Session owner's End User ID. Map it to your own account.
    user_id: str
    #: When the platform stops waiting (aware, UTC). Abandon the work after it.
    deadline: datetime
    #: Always 1: the platform never retries a call.
    attempt: Literal[1]


class ConnectorAuthError(TiDBLinkError):
    """A Connector callback was refused: bad signature, wrong scope or expired."""


def _non_negative_seconds(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number of seconds")
    if not 0 <= value < math.inf:
        raise ValueError(f"{name} must be a non-negative number of seconds")
    return float(value)


def _base64url_bytes(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant {value}")


def _decode_json(part: str) -> dict[str, Any]:
    value = json.loads(_base64url_bytes(part).decode("utf-8", errors="replace"), parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def _safe_integer(value: object) -> bool:
    """JavaScript's ``Number.isSafeInteger`` for a JSON value."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) <= _MAX_SAFE_INTEGER
    if isinstance(value, float):
        return math.isfinite(value) and value.is_integer() and abs(value) <= _MAX_SAFE_INTEGER
    return False


class ConnectorKeySet:
    """Caches the deployment's public verification keys from ``GET /api/v1/connectors/jwks``.

    ``base_url`` is your trusted TiDB Link deployment, for example
    ``https://api.example.com``. Fetch keys only from it, never from a URL a
    token names. ``http_client`` replaces the transport (by default a
    short-lived client per fetch). Fetched keys are reused for ``max_age``
    seconds (default 10 minutes); an unknown key ID refreshes them at most once
    per ``min_refresh_interval`` seconds (default 30).
    """

    def __init__(
        self,
        base_url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        max_age: float = 600.0,
        min_refresh_interval: float = 30.0,
    ) -> None:
        if not isinstance(base_url, str):
            raise TypeError("base_url must be an HTTP(S) URL")
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("base_url must be an HTTP(S) URL")
        if http_client is not None and not isinstance(http_client, httpx.AsyncClient):
            raise TypeError("http_client must be an httpx.AsyncClient")
        self._url = urljoin(base_url, "/api/v1/connectors/jwks")
        self._client = http_client
        self._max_age = _non_negative_seconds(max_age, "max_age")
        self._min_refresh_interval = _non_negative_seconds(min_refresh_interval, "min_refresh_interval")
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._fetched_at = -math.inf
        self._loading: asyncio.Future[None] | None = None

    @property
    def url(self) -> str:
        """Where the keys are fetched from."""
        return self._url

    async def key(self, kid: str) -> Ed25519PublicKey | None:
        """The key for ``kid``, refreshing the cache when it is stale or the key is new."""
        age = time.monotonic() - self._fetched_at
        if age > self._max_age or (kid not in self._keys and age > self._min_refresh_interval):
            await self.refresh()
        return self._keys.get(kid)

    async def refresh(self) -> None:
        """Fetch the keys now. Concurrent callers share one request."""
        loading = self._loading
        if loading is None:
            loading = asyncio.ensure_future(self._load())
            self._loading = loading
            loading.add_done_callback(self._loaded)
        # A cancelled caller does not cancel the request the others share.
        await asyncio.shield(loading)

    def _loaded(self, future: asyncio.Future[None]) -> None:
        if self._loading is future:
            self._loading = None
        if not future.cancelled():
            future.exception()  # Retrieved here so that an unawaited failure is not reported.

    async def _load(self) -> None:
        if self._client is not None:
            await self._fetch(self._client)
            return
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            await self._fetch(client)

    async def _fetch(self, client: httpx.AsyncClient) -> None:
        try:
            response = await client.get(
                self._url, headers={"accept": "application/json"}, follow_redirects=False
            )
        except httpx.HTTPError as cause:
            raise TiDBLinkError("Connector verification keys are unreachable", cause=cause) from cause
        if response.is_redirect:
            raise TiDBLinkError("Connector verification keys are unreachable: redirects are refused")
        if not response.is_success:
            raise TiDBLinkError(
                f"Connector verification keys are unavailable (status {response.status_code})"
            )
        try:
            body = json.loads(response.content)
        except ValueError as cause:
            raise TiDBLinkError("Connector verification keys are malformed", cause=cause) from cause
        listed = body.get("keys") if isinstance(body, dict) else None
        if not isinstance(listed, list):
            raise TiDBLinkError("Connector verification keys are malformed")
        keys: dict[str, Ed25519PublicKey] = {}
        for key in listed:
            if (
                not isinstance(key, dict)
                or key.get("kty") != "OKP"
                or key.get("crv") != "Ed25519"
                or key.get("alg") != "EdDSA"
                or key.get("use") != "sig"
                or not isinstance(key.get("kid"), str)
                or not isinstance(key.get("x"), str)
            ):
                continue
            try:
                keys[key["kid"]] = Ed25519PublicKey.from_public_bytes(_base64url_bytes(key["x"]))
            except (ValueError, binascii.Error) as cause:
                raise TiDBLinkError("Connector verification keys are malformed", cause=cause) from cause
        self._keys = keys
        self._fetched_at = time.monotonic()


async def verify_connector_callback(
    authorization: str | None,
    *,
    keys: ConnectorKeySet,
    organization_id: str,
    connector_id: str,
    clock_tolerance: float = 5.0,
    now: datetime | None = None,
) -> ConnectorCallbackClaims:
    """Verify the ``Authorization`` header of a request the platform sends to your HTTP Connector.

    That covers initialization, tool discovery and tool calls. Returns the
    claims, or raises ConnectorAuthError. Tokens for another Organization
    (``organization_id``) or Connector (``connector_id``) are refused;
    ``clock_tolerance`` is the allowed clock difference with the platform in
    seconds, and ``now`` overrides the current time. A token may be reused
    within its scope until it expires, so verification needs no per-request state.
    """
    if not isinstance(keys, ConnectorKeySet):
        raise TypeError("keys must be a ConnectorKeySet")
    for name, value in (("organization_id", organization_id), ("connector_id", connector_id)):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be text")
        if not value:
            raise ValueError(f"{name} must not be empty")
    tolerance = _non_negative_seconds(clock_tolerance, "clock_tolerance")
    if now is not None:
        if not isinstance(now, datetime):
            raise TypeError("now must be a datetime")
        if now.tzinfo is None:
            raise ValueError("now must be an aware datetime")

    def refuse(reason: str) -> NoReturn:
        raise ConnectorAuthError(f"Connector callback refused: {reason}")

    bearer = _BEARER.fullmatch(authorization) if isinstance(authorization, str) else None
    if not bearer:
        refuse("missing or malformed bearer token")
    header_part, payload_part, signature_part = bearer.group(1).split(".")
    try:
        header = _decode_json(header_part)
        claims = _decode_json(payload_part)
    except (ValueError, binascii.Error):
        refuse("undecodable token")
    kid = header.get("kid")
    if header.get("alg") != "EdDSA" or not isinstance(kid, str):
        refuse("unsupported algorithm")
    key = await keys.key(kid)
    if key is None:
        refuse("unknown signing key")
    try:
        key.verify(_base64url_bytes(signature_part), f"{header_part}.{payload_part}".encode("ascii"))
    except (InvalidSignature, ValueError, binascii.Error):
        refuse("invalid signature")
    current = (now if now is not None else datetime.now(timezone.utc)).timestamp()
    iat, exp = claims.get("iat"), claims.get("exp")
    if claims.get("iss") != CONNECTOR_CALLBACK_ISSUER or claims.get("aud") != CONNECTOR_CALLBACK_AUDIENCE:
        refuse("wrong issuer or audience")
    if claims.get("organizationId") != organization_id or claims.get("connectorId") != connector_id:
        refuse("issued for another Organization or Connector")
    if (
        not _safe_integer(iat)
        or not _safe_integer(exp)
        or exp <= iat  # type: ignore[operator]
        or exp - iat > 60  # type: ignore[operator]
        or iat > current + tolerance  # type: ignore[operator]
        or exp <= current - tolerance  # type: ignore[operator]
    ):
        refuse("expired or not yet valid")
    return claims  # type: ignore[return-value]


def _text(value: object) -> bool:
    return isinstance(value, str) and len(value) > 0


def _parse_deadline(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_connector_call_context(meta: object, *, organization_id: str) -> ConnectorCallContext:
    """Read the Call Context from a ``tools/call`` request's ``params._meta``.

    Raises ConnectorAuthError when it is missing, malformed or for another
    Organization. Initialization and tool discovery carry no Call Context.
    """
    context = meta.get(CONNECTOR_CALL_CONTEXT_KEY) if isinstance(meta, Mapping) else None
    if not isinstance(context, Mapping):
        raise ConnectorAuthError("Connector call has no valid Call Context")
    attempt = context.get("attempt")
    deadline = context.get("deadline")
    parsed = _parse_deadline(deadline) if _text(deadline) else None
    if (
        not _text(context.get("callId"))
        or not _text(context.get("sessionId"))
        or not _text(context.get("userId"))
        or parsed is None
        or isinstance(attempt, bool)
        or not isinstance(attempt, (int, float))
        or attempt != 1
    ):
        raise ConnectorAuthError("Connector call has no valid Call Context")
    if context.get("organizationId") != organization_id:
        raise ConnectorAuthError("Connector call is for another Organization")
    return ConnectorCallContext(
        call_id=context["callId"],
        session_id=context["sessionId"],
        organization_id=context["organizationId"],
        user_id=context["userId"],
        deadline=parsed,
        attempt=1,
    )
