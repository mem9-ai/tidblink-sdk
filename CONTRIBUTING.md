# Contributing

## Requirements

- Node.js 22.12 or newer
- npm 11 or newer
- Python 3.11 or newer, for the Python SDK

## Local checks

```sh
npm ci
npm run test:release
npm run typecheck
npm test
npm run pack:check
```

The frozen selected SDK contract checks use committed exact-service snapshots:

```sh
npm run test:service-contract
```

For actual compatibility across the supported package surface,
run `npm run test:service` with the exact service checkout and a dedicated TiDB
test database; setup is in [the package README](typescript/README.md#compatibility-and-verification).
This verifies the installed packed SDK and its examples against the actual service.
The service runner discovers each `*.fixture.ts` with neighboring `*.cases.mjs`
and runs those distinct dependency lifecycles sequentially. `*.compiled.fixture.ts`
fixtures rebuild the selected backend and use its system harness; other fixtures
retain the AppModule test seam. Every case must have
exactly one fixture owner. The default HTTP tests supplement reproducible failure cases. Keep the
package free of runtime dependencies and do not commit credentials, recorded
production responses, or plaintext API Keys.

TiDB Link OpenAPI is the public contract source. CI verifies the selected frozen contracts. The service revision and OpenAPI
fingerprint in `typescript/package.json` identify the actual-service baseline.
Contract snapshots supplement actual-service execution evidence.
A public API change updates that service contract first, then updates the SDK,
its compatibility declaration, tests, quick start, and release notes together.
A change to the public package surface adds an entry to the unreleased section
of `CHANGELOG.md`. Releases follow [RELEASING.md](RELEASING.md).

Open an issue before adding a new product area. The SDK intentionally exposes a
narrow supported surface rather than every TiDB Link route.

## Gated features

A feature TiDB Link enables only for some Organizations, such as Agents, lives
under `typescript/src/gated/`, one file per feature, and is exported from the
`@tidblink/server/gated` entry only. Keep it removable:

- Gated code may import the root modules; root modules never import gated
  ones. A root module that a gated feature needs exposes an internal function
  that `src/index.ts` does not export, like `endUserHttp` in `src/user.ts`.
  `test/gated-agents.test.mjs` checks that the root entry neither exports nor
  imports the gated entry. In Python the gated code is `tidblink_server.gated`,
  which `tidblink_server` does not import; `tests/test_gated_agents.py` checks
  the same.
- Gated features take the stable client, as in `agents(user)`, and return the
  root types, such as an ordinary `Session`; they add no methods to root
  classes.
- The service's refusal code for each feature appears only in
  `src/gated/errors.ts`, which maps it to `FeatureNotEnabledError`.
- Each feature has its own frozen contract (`service-contract/gated-<feature>.json`,
  registered in `scripts/refresh-contract.mjs`) and its own service fixture
  under `typescript/test/service/gated/`, which enables one Organization and
  leaves another not enabled.

To remove a feature, delete in one change its file under `src/gated/` and its
line in `src/gated/index.ts`, its refusal code in `src/gated/errors.ts`, its
contract snapshot, contract test and registration, its unit tests, service
cases and the parts of the packed-consumer test that use it, its README
subsection and its Python counterpart. Keep the `gated` entry and
`FeatureNotEnabledError` for the next feature.

## Python SDK

The Python SDK in `python/` follows the TypeScript SDK: the same supported
surface, behaviour and service baseline, in Python's naming style. A change to
the TypeScript SDK's public behaviour updates the Python SDK in the same change,
or names the follow-up. Its checks:

```sh
cd python
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## Examples

Each SDK keeps its examples in `{language}/examples/`, covered by its tests;
[`examples/README.md`](examples/README.md) indexes them by scenario. A new SDK
includes the [smart lamp example](examples/lamp/README.md), which declares the
Client Tools in [`examples/lamp/tools.json`](examples/lamp/tools.json) and has
the tests that specification lists. A change to the lamp's behaviour or tools
updates the specification, `tools.json` and every SDK's lamp example together.
