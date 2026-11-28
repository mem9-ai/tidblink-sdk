# Examples

Each SDK keeps its examples in its own `{language}/examples/` directory, next to
the package they use. That way they ship with the package, build with the
language's own toolchain, and run in that SDK's tests against the real service.
This page indexes them by scenario so the same task can be compared across
languages.

## Scenarios

| Scenario | What it shows | TypeScript | Python |
|---|---|---|---|
| [Smart lamp](lamp/README.md) | One Session per device, shared by typed messages and voice; device functions as Client Tools; Realtime grants for the device; a Template per device model, switched when the model changes | [lamp-backend.mjs](../typescript/examples/lamp-backend.mjs) | [lamp_backend.py](../python/examples/lamp_backend.py) |
| Create a Session | Create a Session from a Template with an idempotent creation key | [create-session.mjs](../typescript/examples/create-session.mjs) | [create_session.py](../python/examples/create_session.py) |
| Send a message | Submit text and wait for the result; recover with the same command identity | [send-message.mjs](../typescript/examples/send-message.mjs) | [send_message.py](../python/examples/send_message.py) |
| Control a Turn | Cancel or retry a Turn from its checkpoint | [control-turn.mjs](../typescript/examples/control-turn.mjs) | [control_turn.py](../python/examples/control_turn.py) |
| Upload a file | Upload a file and attach it to a message | [upload-file.mjs](../typescript/examples/upload-file.mjs) | [upload_file.py](../python/examples/upload_file.py) |
| Submit audio | Submit prerecorded audio as a Turn | [submit-audio.mjs](../typescript/examples/submit-audio.mjs) | [submit_audio.py](../python/examples/submit_audio.py) |
| Prepare Realtime | Prepare a Session's Realtime connection for a terminal | [prepare-realtime-connection.mjs](../typescript/examples/prepare-realtime-connection.mjs) | [prepare_realtime_connection.py](../python/examples/prepare_realtime_connection.py) |
| Read usage | Read a User's usage by period and billing tag | [read-usage.mjs](../typescript/examples/read-usage.mjs) | [read_usage.py](../python/examples/read_usage.py) |
| Serve a Connector | Serve a cloud capability to every Session over the Backend Connection; answer with the calling user's data | [serve-connector.mjs](../typescript/examples/serve-connector.mjs) | [serve_connector.py](../python/examples/serve_connector.py) |
| Flash a device | Receive the file the Agent built with a File Notification; a Client Tool hands the device a keyless download ticket pinned to the announced version, and the device's answer returns to the model in the same Turn; the device stays connected until the Turn ends, through a build that takes minutes | [flash-device.mjs](../typescript/examples/flash-device.mjs) | [flash_device.py](../python/examples/flash_device.py) |

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
