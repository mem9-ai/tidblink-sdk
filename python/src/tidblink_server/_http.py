"""The HTTP boundary every REST call goes through (``http.ts`` in the TypeScript SDK)."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote, urlencode, urljoin, urlsplit

import httpx

from ._types import OMIT, Omittable
from .errors import (
    ConflictError,
    DriveFileVersionConflictError,
    OutcomeUnknownError,
    TiDBLinkApiError,
    TiDBLinkConnectionError,
    TiDBLinkError,
)

if TYPE_CHECKING:
    from ._backend_pool import BackendConnectionPool

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
# The largest piece of caller bytes handed to the transport at once.
_BODY_CHUNK = 64 * 1024

RetryMode = Literal["never", "safe"]

_CREATION_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_IDEMPOTENCY_KEY = re.compile(r"[\x20-\x7e]{1,255}")
_USER_ID = re.compile(r"[a-z0-9_-]{1,64}")
_ORGANIZATION_API_KEY = re.compile(r"ti_org_[A-Za-z0-9]{1,64}_[A-Za-z0-9_-]{43}")


def segment(value: str) -> str:
    """Encode one path segment like JavaScript's ``encodeURIComponent``."""
    return quote(value, safe="!'()*-._~")


def query_string(params: Mapping[str, Any]) -> str:
    """``?a=b&...`` for the parameters that are not ``None``, or an empty string."""
    items = [(key, str(value)) for key, value in params.items() if value is not None]
    return f"?{urlencode(items)}" if items else ""


def pagination_query(limit: int | None = None, cursor: str | None = None) -> str:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
        raise TypeError("limit must be an integer")
    if cursor is not None and not isinstance(cursor, str):
        raise TypeError("cursor must be text")
    return query_string({"limit": limit, "cursor": cursor})


def utf16_length(text: str) -> int:
    """The length JavaScript reports for ``text``, in UTF-16 code units, which the service's limits count."""
    return len(text) + sum(1 for character in text if ord(character) > 0xFFFF)


def require_creation_key(value: object, label: str = "Creation", field: str = "idempotency_key") -> str:
    """Check a stable command key; ``label`` names the operation and ``field`` the parameter in the error."""
    if not isinstance(value, str) or not _CREATION_KEY.fullmatch(value):
        error = TypeError if not isinstance(value, str) else ValueError
        raise error(f"{label} requires a stable {field} (1-128 safe characters)")
    return value


def require_template_id(value: object, label: str) -> str:
    """Check a Template ID the SDK sends: 1-64 characters without surrounding whitespace."""
    if (
        not isinstance(value, str)
        or not value
        or utf16_length(value) > 64
        or value.strip() != value
    ):
        error = TypeError if not isinstance(value, str) else ValueError
        raise error(
            f"{label} requires a Template ID of 1-64 characters without surrounding whitespace"
        )
    return value


def template_filter(template_id: str | None) -> dict[str, str]:
    """An optional ``template_id`` filter under its wire name, ``agentTemplateId``."""
    if template_id is None:
        return {}
    return {"agentTemplateId": require_template_id(template_id, "The template_id filter")}


def require_user_id(value: object, message: str) -> str:
    if not isinstance(value, str):
        raise TypeError(message)
    if not _USER_ID.fullmatch(value):
        raise ValueError(message)
    return value


def require_known_keys(value: object, allowed: tuple[str, ...], label: str) -> dict[str, Any]:
    """Check that a JSON object the caller passes names only known fields."""
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    for key in value:
        if key not in allowed:
            raise ValueError(f"{label}.{key} is not supported")
    return dict(value)


def is_organization_api_key(value: object) -> bool:
    """Check the frozen wire grammar without retaining or exposing the credential."""
    return isinstance(value, str) and bool(_ORGANIZATION_API_KEY.fullmatch(value))


def normalize_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("base_url must be text")
    parts = urlsplit(value)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
    ):
        raise ValueError("base_url must be an HTTP(S) URL without credentials")
    path = parts.path or "/"
    return f"{parts.scheme}://{parts.netloc.lower()}{path}".rstrip("/")


@dataclass(frozen=True)
class HttpResult:
    data: Any
    etag: str | None
    request_id: str


def require_etag(result: HttpResult) -> str:
    if not result.etag:
        raise TiDBLinkError(
            "TiDB Link response omitted a required ETag", request_id=result.request_id
        )
    return result.etag


# --- Byte bodies -------------------------------------------------------------


class _Unsent:
    """Marks an error that stopped an upload before it sent any body bytes."""


def mark_unsent(error: BaseException) -> BaseException:
    """Record ``error`` as stopping an upload request before it sent any body bytes."""
    try:
        error._tidblink_unsent = True  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - exceptions always take attributes
        pass
    return error


def is_unsent_failure(error: BaseException) -> bool:
    """Whether ``error`` stopped an upload request before it sent any body bytes."""
    return getattr(error, "_tidblink_unsent", False) is True


def _changed_size() -> ValueError:
    error = ValueError("Upload bytes changed size after they were hashed")
    mark_unsent(error)
    return error


Bytes = bytes | bytearray | memoryview


def byte_view(value: object, label: str = "bytes") -> memoryview:
    """A flat byte view of the caller's bytes, without a copy."""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"{label} must be bytes, bytearray or memoryview")
    view = memoryview(value)
    if not view.c_contiguous:
        raise ValueError(f"{label} must be contiguous")
    return view.cast("B") if view.format != "B" or view.ndim != 1 else view


def require_hashed_length(view: memoryview, length: int) -> None:
    """Refuse bytes that no longer have the length that was hashed."""
    if view.nbytes != length:
        raise _changed_size()


class BytesBody:
    """A request body that sends the caller's bytes in place.

    The transport pulls the bytes only when it sends them, so a body that was
    never pulled shows that no body bytes were sent, and the service cannot
    have received the complete request. It can be sent only once.
    """

    def __init__(self, view: memoryview, length: int) -> None:
        self._view = view
        self._length = length
        self.pulled = False
        self.error: BaseException | None = None

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.pulled:
            raise RuntimeError("A byte body can be sent only once")
        if self._view.nbytes != self._length:
            self.error = _changed_size()
            raise self.error
        self.pulled = True
        for start in range(0, self._length, _BODY_CHUNK):
            yield self._view[start : start + _BODY_CHUNK]  # type: ignore[misc]


def bytes_request(
    view: memoryview, headers: Mapping[str, str], length: int
) -> tuple[BytesBody, dict[str, str]]:
    """The body and headers that send ``view`` without a copy, with the ``content-length`` that was hashed."""
    require_hashed_length(view, length)
    return BytesBody(view, length), {**headers, "content-length": str(length)}


def unsent_body(body: object) -> BaseException | Literal["unsent"] | None:
    """The local error that stopped ``body``, ``"unsent"`` when it was never read, or None."""
    if not isinstance(body, BytesBody):
        return None
    if body.error is not None:
        return body.error
    return None if body.pulled else "unsent"


# --- Transport ---------------------------------------------------------------


def default_http_client() -> httpx.AsyncClient:
    """The transport an ``OrganizationClient`` creates when none is passed.

    Like ``fetch`` in Node.js it has no read deadline: event and audio streams
    stay open for as long as the service sends.
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(None, connect=30.0), follow_redirects=False)


class _RedirectRefused(Exception):
    pass


def _new_request_id() -> str:
    return str(uuid.uuid4())


async def _error_input(response: httpx.Response, fallback_request_id: str) -> dict[str, Any]:
    value: Any
    try:
        value = json.loads(await response.aread())
    except Exception:
        value = None
    error = value.get("error") if isinstance(value, dict) else None
    if not isinstance(error, dict):
        error = None
    result: dict[str, Any] = {
        "status": response.status_code,
        "code": error["code"] if error and isinstance(error.get("code"), str) else "http_error",
        "message": error["message"]
        if error and isinstance(error.get("message"), str)
        else f"TiDB Link request failed with status {response.status_code}",
        "request_id": response.headers.get("x-request-id")
        or (error["requestId"] if error and isinstance(error.get("requestId"), str) else fallback_request_id),
    }
    if error and "details" in error:
        result["details"] = error["details"]
    if "retry-after" in response.headers:
        result["retry_after"] = response.headers["retry-after"]
    return result


async def api_error_from(response: httpx.Response, fallback_request_id: str) -> TiDBLinkApiError:
    """The service's error answer as an error: conflicts as :class:`ConflictError` or a subclass."""
    details = await _error_input(response, fallback_request_id)
    if details["status"] == 409 and details["code"] == "drive_file_version_conflict":
        return DriveFileVersionConflictError(**details)
    if details["status"] in (409, 412):
        return ConflictError(**details)
    return TiDBLinkApiError(**details)


def _dump_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


class HttpClient:
    """Sends requests under one path scope with the Organization API Key.

    The root scope is the Organization API; :meth:`for_user` narrows it to one
    End User's resources. Request paths are relative to the scope.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        http_client: httpx.AsyncClient,
        scope: str = "/api/v1/org",
        backend: BackendConnectionPool | None = None,
        user_id: str | None = None,
    ) -> None:
        self._base_url = normalize_base_url(base_url)
        self._api_key = api_key
        self._scope = scope
        self._client = http_client
        #: The OrganizationClient's Backend Connections, shared by every scope.
        self.backend = backend
        #: The End User this scope selects, if any.
        self.user_id = user_id

    @property
    def client(self) -> httpx.AsyncClient:
        """The transport every request uses, including file transfers to presigned URLs, which carry no API Key."""
        return self._client

    @property
    def base_url(self) -> str:
        return self._base_url

    def for_user(self, user_id: str) -> HttpClient:
        require_user_id(user_id, "Organization resources require an explicit User ID")
        return HttpClient(
            base_url=self._base_url,
            api_key=self._api_key,
            http_client=self._client,
            scope=f"{self._scope}/users/{segment(user_id)}",
            backend=self.backend,
            user_id=user_id,
        )

    def resolve_url(self, value: str) -> str:
        return urljoin(f"{self._base_url}/", value)

    # JSON helpers -------------------------------------------------------------

    async def get(self, path: str, *, request_id: str | None = None) -> Any:
        return (await self.response(path, request_id=request_id, retry="safe")).data

    async def get_response(self, path: str, *, request_id: str | None = None) -> HttpResult:
        return await self.response(path, request_id=request_id, retry="safe")

    async def post(
        self,
        path: str,
        body: Any = OMIT,
        *,
        idempotent: bool = False,
        outcome_unknown: bool = False,
        request_id: str | None = None,
    ) -> Any:
        return (
            await self.post_response(
                path,
                body,
                idempotent=idempotent,
                outcome_unknown=outcome_unknown,
                request_id=request_id,
            )
        ).data

    async def post_response(
        self,
        path: str,
        body: Any = OMIT,
        *,
        idempotency_key: str | None = None,
        if_match: str | None = None,
        idempotent: bool = False,
        outcome_unknown: bool = False,
        request_id: str | None = None,
    ) -> HttpResult:
        return await self.response(
            path,
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
            if_match=if_match,
            outcome_unknown=outcome_unknown,
            request_id=request_id,
            retry="safe" if idempotent else "never",
        )

    async def patch_response(
        self,
        path: str,
        body: Any,
        *,
        if_match: str | None = None,
        outcome_unknown: bool = False,
        request_id: str | None = None,
    ) -> HttpResult:
        return await self.response(
            path,
            method="PATCH",
            body=body,
            if_match=if_match,
            outcome_unknown=outcome_unknown,
            request_id=request_id,
        )

    async def delete_response(
        self, path: str, *, outcome_unknown: bool = False, request_id: str | None = None
    ) -> HttpResult:
        return await self.response(
            path, method="DELETE", outcome_unknown=outcome_unknown, request_id=request_id
        )

    async def response(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Omittable[Any] = OMIT,
        idempotency_key: str | None = None,
        if_match: str | None = None,
        request_id: str | None = None,
        retry: RetryMode = "never",
        outcome_unknown: bool = False,
    ) -> HttpResult:
        content: bytes | None = None
        headers: dict[str, str] = {}
        if body is not OMIT:
            content = _dump_json(body)
            headers["content-type"] = "application/json"
        response, stable_request_id = await self.raw_response(
            path,
            method=method,
            content=content,
            headers=headers,
            idempotency_key=idempotency_key,
            if_match=if_match,
            request_id=request_id,
            retry=retry,
            outcome_unknown=outcome_unknown,
        )
        try:
            raw = await response.aread()
        except httpx.TransportError as cause:
            raise _unusable(outcome_unknown, stable_request_id, cause) from cause
        finally:
            await response.aclose()
        try:
            data = None if response.status_code == 204 else json.loads(raw)
        except ValueError as cause:
            raise _unusable(outcome_unknown, stable_request_id, cause) from cause
        return HttpResult(data=data, etag=response.headers.get("etag") or None, request_id=stable_request_id)

    async def raw_response(
        self,
        path: str,
        *,
        method: str = "GET",
        content: bytes | BytesBody | None = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        if_match: str | None = None,
        request_id: str | None = None,
        retry: RetryMode = "never",
        outcome_unknown: bool = False,
    ) -> tuple[httpx.Response, str]:
        """Send a request and return its successful response, still streaming.

        The caller reads and closes the response (``aread``/``aiter_bytes`` and
        ``aclose``). Raw bytes and event streams retain the same credential,
        redirect and error boundary as JSON calls.
        """
        stable_request_id = request_id if request_id is not None else _new_request_id()
        if not isinstance(stable_request_id, str) or not _REQUEST_ID.fullmatch(stable_request_id):
            raise ValueError("request_id must be 1-64 safe characters")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key)
        ):
            raise ValueError("idempotency_key must be 1-255 visible ASCII characters")
        if retry == "safe" and isinstance(content, BytesBody):
            raise ValueError("A streamed request body cannot be retried")
        attempts = MAX_ATTEMPTS if retry == "safe" else 1
        request_headers = {
            "accept": "application/json",
            **(headers or {}),
            "authorization": f"Bearer {self._api_key}",
            "x-request-id": stable_request_id,
        }
        if idempotency_key:
            request_headers["idempotency-key"] = idempotency_key
        if if_match:
            request_headers["if-match"] = if_match
        url = f"{self._base_url}{self._scope}{path}"
        for attempt in range(1, attempts + 1):
            try:
                request = self._client.build_request(
                    method, url, headers=request_headers, content=content
                )
                response = await self._client.send(request, stream=True, follow_redirects=False)
                if response.is_redirect:
                    await response.aclose()
                    raise _RedirectRefused("TiDB Link answered with a redirect, which is refused")
            except (httpx.TransportError, _RedirectRefused) as cause:
                unsent = unsent_body(content)
                if isinstance(unsent, BaseException):
                    raise unsent from cause
                # The service cannot have received a request with no body bytes.
                unknown = outcome_unknown and not unsent
                if attempt < attempts:
                    await asyncio.sleep(0.1 * 2 ** (attempt - 1))
                    continue
                error: TiDBLinkError
                if unknown:
                    error = OutcomeUnknownError(
                        "TiDB Link request outcome is unknown; do not repeat it automatically",
                        request_id=stable_request_id,
                        cause=cause,
                    )
                else:
                    error = TiDBLinkConnectionError(
                        "Could not reach TiDB Link", request_id=stable_request_id, cause=cause
                    )
                if unsent:
                    mark_unsent(error)
                raise error from cause
            except BaseException as cause:
                unsent = unsent_body(content)
                if isinstance(unsent, BaseException) and unsent is not cause:
                    raise unsent from cause
                if unsent == "unsent":
                    mark_unsent(cause)
                raise

            if response.is_success:
                return response, response.headers.get("x-request-id") or stable_request_id

            if attempt < attempts and response.status_code in RETRYABLE_STATUS:
                try:
                    await response.aread()
                except Exception:
                    pass
                finally:
                    await response.aclose()
                await asyncio.sleep(0.1 * 2 ** (attempt - 1))
                continue

            try:
                api_error = await api_error_from(response, stable_request_id)
            finally:
                await response.aclose()
            if outcome_unknown and (api_error.status >= 500 or api_error.status == 408):
                raise OutcomeUnknownError(
                    "TiDB Link could not confirm the mutation outcome",
                    request_id=api_error.request_id,
                    cause=api_error,
                ) from api_error
            raise api_error
        raise AssertionError("unreachable")


def _unusable(outcome_unknown: bool, request_id: str, cause: BaseException) -> TiDBLinkError:
    if outcome_unknown:
        return OutcomeUnknownError(
            "TiDB Link accepted the request but returned an unusable response",
            request_id=request_id,
            cause=cause,
        )
    return TiDBLinkError("TiDB Link returned invalid JSON", request_id=request_id, cause=cause)
