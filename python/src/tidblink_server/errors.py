"""Errors raised by the SDK.

Every error the SDK defines derives from :class:`TiDBLinkError`. Invalid
arguments raise :class:`TypeError` (wrong type) or :class:`ValueError` (wrong
value) before any request is sent.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "TiDBLinkError",
    "TiDBLinkApiError",
    "ConflictError",
    "DriveFileVersionConflictError",
    "TiDBLinkConnectionError",
    "OutcomeUnknownError",
    "TurnWaitTimeoutError",
    "InputNotAppliedError",
    "TurnFailedError",
    "TurnInterruptedError",
    "BackendConnectionClosedError",
    "BackendRequestError",
]


class TiDBLinkError(Exception):
    """Base class of every SDK error. ``request_id`` is the HTTP request identity, when known."""

    request_id: str | None

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        if cause is not None:
            self.__cause__ = cause

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""

    @property
    def cause(self) -> BaseException | None:
        """The error this one was raised from, if any (``__cause__``)."""
        return self.__cause__


class TiDBLinkApiError(TiDBLinkError):
    """The service answered with an error: its HTTP status, error code, details and ``Retry-After``."""

    status: int
    code: str
    details: Any
    retry_after: str | None

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        request_id: str | None = None,
        details: Any = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.status = status
        self.code = code
        self.details = details
        self.retry_after = retry_after


class ConflictError(TiDBLinkApiError):
    """An explicit conflict: HTTP 409, or 412 for a stale ETag."""


class DriveFileVersionConflictError(ConflictError):
    """A file in a Session's Drive is no longer at the version a read or download ticket was pinned to.

    The service's code is ``drive_file_version_conflict``: the Agent changed
    the file after announcing it. A missing file or Session is a 404
    :class:`TiDBLinkApiError` instead.
    """


class TiDBLinkConnectionError(TiDBLinkError):
    """The service could not be reached. (``ConnectionError`` in the TypeScript SDK.)"""


class OutcomeUnknownError(TiDBLinkError):
    """The request may have succeeded, so repeating it could create another effect."""

    turn_id: str | None
    session_id: str | None
    input_id: str | None

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        turn_id: str | None = None,
        session_id: str | None = None,
        input_id: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id, cause=cause)
        self.turn_id = turn_id
        self.session_id = session_id
        self.input_id = input_id


class TurnWaitTimeoutError(OutcomeUnknownError):
    """A local wait reached its deadline. The Turn was not cancelled."""


class InputNotAppliedError(TiDBLinkError):
    """The original message was refused, or succeeded without being incorporated."""

    def __init__(self, session_id: str, input_id: str, turn_id: str) -> None:
        super().__init__(
            "The original message was not incorporated; inspect its interaction receipt"
        )
        self.session_id = session_id
        self.input_id = input_id
        self.turn_id = turn_id


class TurnFailedError(TiDBLinkError):
    code: str
    turn_id: str
    session_id: str | None
    input_id: str | None

    def __init__(
        self,
        *,
        code: str,
        message: str,
        turn_id: str,
        session_id: str | None = None,
        input_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.turn_id = turn_id
        self.session_id = session_id
        self.input_id = input_id


class TurnInterruptedError(TiDBLinkError):
    def __init__(
        self,
        turn_id: str,
        message: str = "Turn was interrupted",
        session_id: str | None = None,
        input_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.turn_id = turn_id
        self.session_id = session_id
        self.input_id = input_id


class BackendConnectionClosedError(TiDBLinkConnectionError):
    """A Backend Connection closed, or could not be opened.

    ``code`` is the WebSocket close code: 4001 means the Organization API Key is
    missing, invalid, revoked or rotated, or its Organization is inactive, and
    reconnecting cannot help (``retryable`` is false). Other closes, such as a
    drained or restarted replica, are retryable.
    """

    code: int
    reason: str
    retryable: bool

    def __init__(
        self,
        *,
        code: int,
        reason: str | None = None,
        message: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message
            if message is not None
            else f"Backend Connection closed with code {code}{f': {reason}' if reason else ''}",
            cause=cause,
        )
        self.code = code
        self.reason = reason or ""
        self.retryable = code not in (4001, 4009)


class BackendRequestError(TiDBLinkError):
    """The core answered a Backend Connection request with a JSON-RPC error.

    ``code`` is the JSON-RPC error code; for -32000 (refused), ``reason`` names
    why, for example ``session_not_found`` or ``connection_full``. A refused
    ``session.send`` carries the REST message command's error code as
    ``reason``, such as ``session_read_only`` or ``turn_admission_closed``, and
    the REST error's ``details``.
    """

    code: int
    reason: str | None
    details: Any

    def __init__(
        self,
        *,
        code: int,
        message: str,
        reason: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.details = details
