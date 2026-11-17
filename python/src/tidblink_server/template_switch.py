"""Switching a Session to another Template (``template-switch.ts`` in the TypeScript SDK)."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from ._http import HttpClient, require_creation_key, require_template_id, segment
from ._types import OMIT, Omittable
from .errors import OutcomeUnknownError, TiDBLinkApiError

#: ``pending``: a Handoff Turn runs under the target Template; ``succeeded``:
#: the Session is on the target; ``failed``: it stays on its previous Template;
#: ``unchanged``: the target matched the current configuration.
TemplateSwitchStatus = Literal["pending", "succeeded", "failed", "unchanged"]
_STATUSES = ("pending", "succeeded", "failed", "unchanged")


class TemplateSwitchError(TypedDict):
    code: str


class TemplateSwitchReceipt(TypedDict):
    commandId: str
    sessionId: str
    #: The target Template.
    agentTemplateId: str
    #: The epoch the switch started from; after ``succeeded``, the new current epoch.
    configurationEpoch: int
    status: TemplateSwitchStatus
    #: The Handoff Turn, a ``kind: "template_handoff"`` Turn. Always present when
    #: ``pending``; absent when ``unchanged``, or when a ``succeeded`` Session had no history.
    handoffTurnId: NotRequired[str]
    #: Present when ``failed``.
    error: NotRequired[TemplateSwitchError]
    createdAt: str
    completedAt: NotRequired[str]


# 503 codes the service returns before it records the switch.
_REFUSED_BEFORE_RECORDING = frozenset({"turn_admission_closed", "skill_package_store_unavailable"})
_RECOVER = "repeat it with the same idempotency_key and input to recover its receipt"


def _is_text(value: object) -> bool:
    return isinstance(value, str) and len(value) > 0


def _is_receipt(value: object, session_id: str, template_id: str) -> bool:
    if not isinstance(value, dict):
        return False
    epoch = value.get("configurationEpoch")
    if (
        not _is_text(value.get("commandId"))
        or value.get("sessionId") != session_id
        or value.get("agentTemplateId") != template_id
        or not isinstance(value.get("createdAt"), str)
        or not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch < 1
        or value.get("status") not in _STATUSES
    ):
        return False
    status = value["status"]
    if status == "pending":
        return _is_text(value.get("handoffTurnId"))
    if status == "failed":
        error = value.get("error")
        return isinstance(error, dict) and _is_text(error.get("code"))
    if status == "unchanged":
        return "handoffTurnId" not in value
    return True


async def switch_template(
    http: HttpClient,
    session_id: str,
    *,
    template_id: str,
    idempotency_key: str,
    acknowledge_session_recall: Omittable[bool] = OMIT,
    request_id: str | None = None,
) -> TemplateSwitchReceipt:
    require_template_id(template_id, "Template switch")
    require_creation_key(idempotency_key, "Template switch")
    if acknowledge_session_recall is not OMIT and not isinstance(acknowledge_session_recall, bool):
        raise TypeError("acknowledge_session_recall must be a boolean")
    body: dict[str, object] = {"agentTemplateId": template_id}
    if acknowledge_session_recall is not OMIT:
        body["acknowledgeSessionRecall"] = acknowledge_session_recall
    try:
        result = await http.post_response(
            f"/sessions/{segment(session_id)}/template-switches",
            body,
            idempotency_key=idempotency_key,
            idempotent=True,
            outcome_unknown=True,
            request_id=request_id,
        )
    except OutcomeUnknownError as error:
        cause = error.__cause__
        if (
            isinstance(cause, TiDBLinkApiError)
            and cause.status == 503
            and cause.code in _REFUSED_BEFORE_RECORDING
        ):
            raise cause from None
        raise OutcomeUnknownError(
            f"Template switch outcome is unknown; {_RECOVER}",
            session_id=session_id,
            request_id=error.request_id,
            cause=error,
        ) from error
    receipt = result.data.get("receipt") if isinstance(result.data, dict) else None
    if not _is_receipt(receipt, session_id, template_id):
        raise OutcomeUnknownError(
            f"Template switch returned no usable receipt; {_RECOVER}",
            request_id=result.request_id,
            session_id=session_id,
        )
    return receipt  # type: ignore[return-value]
