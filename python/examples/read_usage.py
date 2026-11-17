"""Read one End User's usage summary and the first page of its usage items."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from tidblink_server import OrganizationClient
from tidblink_server import BillingUsageSeriesInterval


async def read_user_usage(
    *,
    base_url: str,
    organization_api_key: str,
    user_id: str,
    from_: str,
    to: str,
    interval: BillingUsageSeriesInterval | None = None,
    tag_key: str | None = None,
    tag_value: str | None = None,
) -> dict[str, Any]:
    """Details return the first page; retain ``nextCursor`` and the original filters."""
    async with OrganizationClient(base_url=base_url, api_key=organization_api_key) as organization:
        filters: dict[str, Any] = {"from_": from_, "to": to, "tag_key": tag_key, "tag_value": tag_value}
        return {
            "summary": await organization.get_user_usage(user_id, interval=interval, **filters),
            "details": await organization.get_user_usage_details(user_id, **filters),
        }


async def main() -> int:
    try:
        result = await read_user_usage(
            base_url=os.environ.get("AGENT_SERVICE_URL", ""),
            organization_api_key=os.environ.get("ORGANIZATION_API_KEY", ""),
            user_id=os.environ.get("END_USER_ID", ""),
            from_=os.environ.get("USAGE_FROM", ""),
            to=os.environ.get("USAGE_TO", ""),
            tag_key=os.environ.get("TAG_KEY"),
            tag_value=os.environ.get("TAG_VALUE"),
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(str(error) or "Usage read failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
