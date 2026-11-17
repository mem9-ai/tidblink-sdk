import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from audio_support import local_server
from support import ORGANIZATION_API_KEY, error_response, json_response, serve
from tidblink_server import TiDBLinkApiError

ROOT = Path(__file__).resolve().parent.parent
RANGE = {"from_": "2030-01-01T00:00:00.000Z", "to": "2030-01-02T00:00:00.000Z"}
DETAILS = {
    "items": [
        {
            "kind": "llm",
            "id": "usage_1",
            "costMicros": None,
            "valuationStatus": "unpriced",
            "pricingStatus": "unknown",
            "valuationReason": "no_rate_authority",
            "turnId": None,
            "realtimeConnectionId": "voice_1",
            "billingTag": None,
        }
    ],
    "nextCursor": "opaque/+?cursor",
    "factCompleteness": {"status": "incomplete", "indeterminateIntentCount": 1},
}
AGGREGATE = {
    "range": {"from": RANGE["from_"], "to": RANGE["to"], "interval": "hour"},
    "totals": {"totalCostMicros": 12},
    "series": [],
}


async def test_organization_usage_targets_an_explicit_user_and_retains_cursor_exact_range_and_distinct_detail_projection() -> None:
    org, service = serve(
        lambda request, service: json_response(DETAILS if "/details" in request.url.path else AGGREGATE)
    )
    assert await org.get_user_usage("user_1", **RANGE, interval="hour", template_id="template_1") == AGGREGATE
    assert (
        await org.get_user_usage_details(
            "user_1",
            **RANGE,
            kind="llm",
            limit=1,
            cursor="opaque/+?cursor",
            template_id="template_1",
            tag_key="team",
            tag_value="R&D",
        )
        == DETAILS
    )
    for call in service.calls:
        assert call.query["agentTemplateId"] == "template_1"
        assert "templateId" not in call.query
    summary, details = service.calls
    assert summary.pathname == "/api/v1/org/users/user_1/billing/usage"
    assert summary.query == {
        "from": RANGE["from_"],
        "to": RANGE["to"],
        "interval": "hour",
        "agentTemplateId": "template_1",
    }
    assert details.pathname == "/api/v1/org/users/user_1/billing/usage/details"
    assert details.query["cursor"] == "opaque/+?cursor"
    assert details.query["tagKey"] == "team"
    assert details.query["tagValue"] == "R&D"
    assert details.query["from"] == RANGE["from_"]
    assert details.query["kind"] == "llm"
    assert details.query["limit"] == "1"
    assert all(call.headers["authorization"] == f"Bearer {ORGANIZATION_API_KEY}" for call in service.calls)
    await org.close()


async def test_a_missing_organization_target_or_unsupported_option_cannot_dispatch_a_read() -> None:
    org, service = serve(lambda request, service: json_response({}))
    with pytest.raises(ValueError):
        await org.get_user_usage("", from_="a", to="b")
    with pytest.raises(TypeError):
        await org.get_user_usage("user_1")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        await org.get_user_usage("user_1", from_="", to="b")
    with pytest.raises(TypeError):
        await org.get_user_usage("user_1", from_=1, to="b")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await org.get_user_usage_details("user_1", from_="a", to="b", interval="hour")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await org.get_user_usage_details("user_1", from_="a", to="b", limit=True)
    with pytest.raises(TypeError):
        await org.get_user_usage_details("user_1", from_="a", to="b", cursor=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await org.get_user_usage_details("user_1", from_="a", to="b", template_id=" padded ")
    assert service.calls == []
    await org.close()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "invalid_billing_usage_query"),
        (400, "invalid_billing_usage_cursor"),
        (404, "billing_target_not_found"),
    ],
)
async def test_invalid_range_mismatched_cursor_and_foreign_target_preserve_service_refusals(
    status: int, code: str
) -> None:
    org, _ = serve(lambda request, service: error_response(status, code, requestId="usage-read-1"))
    with pytest.raises(TiDBLinkApiError) as caught:
        await org.get_user_usage_details("user_1", **RANGE, cursor="foreign")
    assert caught.value.code == code
    assert caught.value.status == status
    assert caught.value.request_id == "usage-read-1"
    await org.close()


# --- Example ----------------------------------------------------------------


def _usage_service(requests: list):
    def handler(method: str, path: str, headers: dict[str, str], body: bytes):
        requests.append((path, headers.get("authorization")))
        value = DETAILS if "/details" in path else AGGREGATE
        return 200, {"content-type": "application/json"}, json.dumps(value).encode()

    return handler


def _query(path: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(path).query)


async def test_read_usage_example_reads_the_summary_and_the_first_details_page() -> None:
    from examples.read_usage import read_user_usage

    requests: list = []
    with local_server(_usage_service(requests)) as base_url:
        result = await read_user_usage(
            base_url=base_url,
            organization_api_key=ORGANIZATION_API_KEY,
            user_id="user_1",
            **RANGE,
            interval="day",
            tag_key="team",
            tag_value="R&D",
        )
    assert result == {"summary": AGGREGATE, "details": DETAILS}
    (summary, key), (details, _) = requests
    assert key == f"Bearer {ORGANIZATION_API_KEY}"
    assert _query(summary)["interval"] == ["day"]
    assert "interval" not in _query(details)
    assert _query(details)["tagValue"] == ["R&D"]


async def test_read_usage_example_main_reads_the_environment() -> None:
    requests: list = []
    with local_server(_usage_service(requests)) as base_url:
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "AGENT_SERVICE_URL": base_url,
            "ORGANIZATION_API_KEY": ORGANIZATION_API_KEY,
            "END_USER_ID": "user_1",
            "USAGE_FROM": RANGE["from_"],
            "USAGE_TO": RANGE["to"],
            "TAG_KEY": "team",
            "TAG_VALUE": "R&D",
        }
        done = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(ROOT / "examples" / "read_usage.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"summary": AGGREGATE, "details": DETAILS}
    assert _query(requests[0][0])["from"] == [RANGE["from_"]]
    assert _query(requests[1][0])["tagKey"] == ["team"]
    failed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(ROOT / "examples" / "read_usage.py")],
        env={**env, "END_USER_ID": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert failed.returncode == 1
    assert "explicit User ID" in failed.stderr
