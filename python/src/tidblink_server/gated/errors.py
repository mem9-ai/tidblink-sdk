"""The refusal of a gated feature (``gated/errors.ts`` in the TypeScript SDK)."""

from __future__ import annotations

from typing import Any, Literal, NoReturn

from ..errors import TiDBLinkApiError

#: A gated feature, as named on :attr:`FeatureNotEnabledError.feature`.
GatedFeature = Literal["agents"]


class FeatureNotEnabledError(TiDBLinkApiError):
    """The service refused a gated feature because the Organization is not enabled for it.

    ``status``, ``code`` and ``request_id`` are the service's own; enabling the
    Organization makes the same request succeed.
    """

    feature: GatedFeature

    def __init__(self, *, feature: GatedFeature, **fields: Any) -> None:
        super().__init__(**fields)
        self.feature = feature


# The service's refusal code for each gated feature. Change it here when the service does.
_REFUSALS: dict[str, tuple[GatedFeature, str]] = {
    "agent_preview_not_enabled": ("agents", "Agents"),
}


def raise_gated(error: BaseException) -> NoReturn:
    """Raise a gated feature's 403 refusal as :class:`FeatureNotEnabledError`, and anything else unchanged."""
    if (
        not isinstance(error, TiDBLinkApiError)
        or isinstance(error, FeatureNotEnabledError)
        or error.status != 403
        or error.code not in _REFUSALS
    ):
        raise error
    feature, label = _REFUSALS[error.code]
    raise FeatureNotEnabledError(
        feature=feature,
        status=error.status,
        code=error.code,
        message=(
            f"{label} are not enabled for this Organization; they are available only to "
            "Organizations TiDB Link has enabled"
        ),
        request_id=error.request_id,
        details=error.details,
        retry_after=error.retry_after,
    ) from error
