"""The package declares the same service baseline as the TypeScript SDK."""

import json
import tomllib
from pathlib import Path

import tidblink_server

ROOT = Path(__file__).resolve().parents[1]


def test_service_baseline_matches_the_typescript_sdk() -> None:
    typescript = json.loads((ROOT.parent / "typescript" / "package.json").read_text())
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["tidblink"]
    assert tidblink_server.TIDB_LINK_API_VERSION == typescript["tidbLinkApiVersion"] == declared["api-version"]
    assert tidblink_server.TIDB_LINK_REVISION == typescript["tidbLinkRevision"] == declared["revision"]
    assert (
        tidblink_server.TIDB_LINK_OPENAPI_SHA256
        == typescript["tidbLinkOpenApiSha256"]
        == declared["openapi-sha256"]
    )


def test_every_exported_name_exists() -> None:
    for name in tidblink_server.__all__:
        assert hasattr(tidblink_server, name), name
    assert len(set(tidblink_server.__all__)) == len(tidblink_server.__all__)


def test_lengths_and_truncation_count_utf16_units_like_the_service() -> None:
    import pytest

    from tidblink_server._client_tool_executor import _truncate
    from tidblink_server._http import require_template_id, utf16_length

    assert utf16_length("a😀") == 3
    require_template_id("😀" * 32, "Test")
    with pytest.raises(ValueError):
        require_template_id("a" + "😀" * 32, "Test")
    cut = _truncate("😀" * 3000, 4096)
    assert utf16_length(cut) <= 4096 and cut.endswith("…") and "\ud83d" not in cut
    assert _truncate("short", 4096) == "short"
