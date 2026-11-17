# Examples

Each SDK keeps its examples in its own `{language}/examples/` directory, next to
the package they use. That way they ship with the package, build with the
language's own toolchain, and run in that SDK's tests against the real service.
This page indexes them by scenario so the same task can be compared across
languages.

## Scenarios

| Scenario | What it shows | TypeScript |
|---|---|---|
| [Smart lamp](lamp/README.md) | One Session per device, shared by typed messages and voice; device functions as Client Tools; Realtime grants for the device | [lamp-backend.mjs](../typescript/examples/lamp-backend.mjs) |
| Create a Session | Create a Session from a Template with an idempotent creation key | [create-session.mjs](../typescript/examples/create-session.mjs) |
| Send a message | Submit text and wait for the result; recover with the same command identity | [send-message.mjs](../typescript/examples/send-message.mjs) |
| Control a Turn | Cancel or retry a Turn from its checkpoint | [control-turn.mjs](../typescript/examples/control-turn.mjs) |
| Upload a file | Upload a file and attach it to a message | [upload-file.mjs](../typescript/examples/upload-file.mjs) |
| Submit audio | Submit prerecorded audio as a Turn | [submit-audio.mjs](../typescript/examples/submit-audio.mjs) |
| Prepare Realtime | Prepare a Session's Realtime connection for a terminal | [prepare-realtime-connection.mjs](../typescript/examples/prepare-realtime-connection.mjs) |
| Read usage | Read a User's usage by period and billing tag | [read-usage.mjs](../typescript/examples/read-usage.mjs) |
| HTTP Connector | A minimal MCP server that verifies platform callbacks | [http-connector.mjs](../typescript/examples/http-connector.mjs) |

Add a column when a new SDK lands, and link each example it provides.

## Rules for every SDK

- **The smart lamp example is required.** Every SDK provides it in
  `{language}/examples/` and follows [its specification](lamp/README.md): the
  same operations, the same device behaviour and the exact tool declarations
  in [`lamp/tools.json`](lamp/tools.json). The SDK's tests check its
  declarations against that file.
- Other scenarios are added as the SDK supports them. Use the scenario names
  above, in the language's naming style (`lamp-backend.mjs`, `lamp_backend.py`,
  `lamp_backend.go`), so the matrix stays easy to read.
- Examples use only the published package, read their settings from the same
  environment variables across languages (`AGENT_SERVICE_URL`,
  `ORGANIZATION_API_KEY`, `END_USER_ID`, `SESSION_ID`, ...), and are covered by
  that SDK's tests.
