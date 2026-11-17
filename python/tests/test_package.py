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
