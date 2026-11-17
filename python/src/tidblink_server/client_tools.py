"""Session Client Tool declarations (``client-tools.ts`` in the TypeScript SDK)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, TypedDict

from ._http import HttpClient, HttpResult, require_etag, require_known_keys, segment
from .errors import OutcomeUnknownError, TiDBLinkError


class ClientToolDeclaration(TypedDict):
    type: Literal["function"]
    name: str
    description: str
    #: JSON Schema of the arguments.
    parameters: dict[str, Any]


class ClientToolSet(TypedDict):
    revision: int
    tools: list[ClientToolDeclaration]
    #: Pass this exact value when replacing the complete set.
    etag: str


def validate_client_tools(tools: Sequence[ClientToolDeclaration]) -> list[ClientToolDeclaration]:
    if isinstance(tools, (str, bytes)) or not isinstance(tools, Sequence) or len(tools) > 64:
        raise ValueError("tools must be a list of at most 64 declarations")
    checked: list[ClientToolDeclaration] = []
    for tool in tools:
        require_known_keys(tool, ("type", "name", "description", "parameters"), "Client Tool declaration")
        if (
            tool.get("type") != "function"
            or not isinstance(tool.get("name"), str)
            or not tool["name"]
            or not isinstance(tool.get("description"), str)
            or not tool["description"]
            or not isinstance(tool.get("parameters"), dict)
        ):
            raise ValueError(
                "Client Tools require a function name, description and JSON Schema parameters"
            )
        checked.append(dict(tool))  # type: ignore[arg-type]
    return checked


def _tool_set(result: HttpResult) -> ClientToolSet:
    data = result.data
    revision = data.get("revision") if isinstance(data, dict) else None
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
        or not isinstance(data.get("tools"), list)
    ):
        raise TiDBLinkError("TiDB Link returned an invalid Client Tool set", request_id=result.request_id)
    return {**data, "etag": require_etag(result)}  # type: ignore[typeddict-item]


def _tools_path(session_id: str) -> str:
    return f"/sessions/{segment(session_id)}/tools"


async def get_client_tools(
    http: HttpClient, session_id: str, *, request_id: str | None = None
) -> ClientToolSet:
    return _tool_set(await http.get_response(_tools_path(session_id), request_id=request_id))


async def replace_client_tools(
    http: HttpClient,
    session_id: str,
    tools: Sequence[ClientToolDeclaration],
    *,
    if_match: str,
    request_id: str | None = None,
) -> ClientToolSet:
    checked = validate_client_tools(tools)
    if not isinstance(if_match, str) or not if_match:
        raise ValueError("Client Tool replacement requires if_match")
    result = await http.response(
        _tools_path(session_id),
        method="PUT",
        body={"tools": checked},
        if_match=if_match,
        outcome_unknown=True,
        request_id=request_id,
    )
    try:
        return _tool_set(result)
    except TiDBLinkError as cause:
        raise OutcomeUnknownError(
            "Client Tool replacement returned no usable receipt; read the current set before retrying",
            session_id=session_id,
            request_id=result.request_id,
            cause=cause,
        ) from cause
