# Smart lamp example

The backend of a smart lamp that users talk to by typing in an app or by
speaking to the lamp. Every SDK provides this example in `{language}/examples/`
with the behaviour below, so the same lamp can be served by a backend in any
language.

| Language | Implementation | Tests |
|---|---|---|
| TypeScript | [lamp-backend.mjs](../../typescript/examples/lamp-backend.mjs) | [lamp-example.test.mjs](../../typescript/test/lamp-example.test.mjs), [real service](../../typescript/test/service/backend-connection.cases.mjs) |
| Python | [lamp_backend.py](../../python/examples/lamp_backend.py) | [test_lamp_example.py](../../python/tests/test_lamp_example.py); the real-service test (5) is not yet written |

The TypeScript implementation is the reference. If this specification and the
reference disagree, fix one of them in the same change.

## Design

```
 app ──typed message──▶ backend (this example, SDK) ◀──Backend Connection──▶ TiDB Link
                          │  ▲                                                 ▲
         device channel   │  │ presence, talk button                           │ audio
         (MQTT, ...)      ▼  │                                                 │
                         lamp ─────────────Realtime WebSocket (grant)──────────┘
```

- Typed and spoken messages share one Session per lamp, so one conversation
  history.
- The lamp's functions are Client Tools. The model calls them, the backend
  runs them by forwarding to the lamp over the application's own device
  channel, and the service never talks to the lamp except for audio.
- While the lamp is online the backend keeps its Session connected, so the
  lamp's tools also work during voice. Typing while the lamp is offline still
  works; the tools then fail at once and the model says so.
- For voice the backend hands the lamp a short-lived Realtime grant. The lamp
  opens the Realtime WebSocket itself; audio never passes through the backend.
- Cloud capabilities, such as a warranty lookup, belong in a Connector that the
  Template references, not in the lamp's tools.
- Each lamp model has its own Template, so a model's personality, instructions
  and Connectors are configured apart from the others. One Organization API Key
  uses any number of the Organization's Templates. A lamp's Session starts on
  its model's Template and switches Template when the lamp changes model, for
  example after an upgrade, keeping its conversation history.

## Inputs

Construction takes the following, named in the language's own style:

| Input | Meaning |
|---|---|
| `baseUrl`, `organizationApiKey` | Create one Organization client, which owns the Backend Connections. |
| `templates` | The Template ID of each lamp model, such as `{ standard, pro }`. |
| `device` | The channel to the lamps, provided by the application (see below). |
| `store` | Lamp bindings and event cursors. Optional; defaults to an in-memory store. |
| `onChat(deviceId, { role, text })` | Optional. Receives voice transcripts (`role: "user"`) and voice responses (`role: "assistant"`) so they also appear in the app. |

`device` has three operations:

- `isOnline(deviceId) -> boolean`
- `request(deviceId, command, args, signal) -> result`, which fails when the lamp
  reports a failure. It must stop when `signal` is cancelled.
- `send(deviceId, topic, payload)`: fire and forget.

`store` has four operations:

- `bind(deviceId, { userId, sessionId })`
- `binding(deviceId) -> { userId, sessionId } | none`
- `cursor(deviceId) -> cursor | none`
- `saveCursor(deviceId, cursor)`

## Operations

| Operation | Behaviour |
|---|---|
| `bindDevice(appUserId, deviceId, model) -> sessionId` | A model that is not in `templates` fails with `Unknown lamp model {model}` before any request. Create the End User with `externalId = appUserId`. This is safe to repeat. Create the lamp's Session from `templates[model]` with idempotency key `lamp.{deviceId}`, so a retry returns the same Session. Binding the lamp again with another model is refused, because the key is the same and the input is not; use `changeModel` instead. Store the binding. |
| `changeModel(deviceId, model, { changeId? }) -> receipt` | A model that is not in `templates` fails with `Unknown lamp model {model}` before any request. Switch the lamp's Session to `templates[model]` with idempotency key `lamp.{deviceId}.{changeId}` and return the switch receipt. `changeId` defaults to a new random ID; pass the same one to recover a lost response. The Session keeps its history: when it has some, the receipt is `pending` while a Handoff Turn runs under the new Template. The lamp's connection and tools stay as they are. A lamp without a binding fails with `Lamp {deviceId} is not bound to a user`. The service refuses the switch while a Turn runs or the lamp is talking. |
| `onDeviceOnline(deviceId)` | If the lamp is already connected here, do nothing. Otherwise connect its Session with the lamp's tools, starting from the stored cursor, saving each new cursor and forwarding voice transcripts and responses to `onChat`. When the connection ends on its own, for example because another process took the lamp over, forget it. A lamp without a binding fails with `Lamp {deviceId} is not bound to a user`. |
| `onDeviceOffline(deviceId)` | Forget the lamp's connection and close it. |
| `onAppMessage(deviceId, text, { clientEventId?, onDraft? }) -> text` | Send the message over the lamp's connection. If the lamp is not connected, use a temporary connection and close it afterwards. Report each growing draft to `onDraft`. Return the final answer's text. |
| `onDeviceWantsToTalk(deviceId) -> grant` | If the lamp is not connected, fail with `Lamp {deviceId} is not online`. Otherwise get a Realtime grant from its connection, send it to the lamp with topic `voice_grant`, and return it. |
| `close()` | Forget every lamp and close the Organization client with its connections. |

## Tools

The tool declarations are in [`tools.json`](tools.json). Every implementation
declares exactly those, in that order, and its tests compare them with the
file. Each tool first checks `device.isOnline(deviceId)` and, when the lamp is
offline, fails at once with `The lamp is offline`. Otherwise it sends this
request to the lamp:

| Tool | Device request | Output to the model |
|---|---|---|
| `set_light` | `set_light` with the call's arguments; the lamp returns its state `{ on, brightness, colorTemp }`. | `The lamp is on at {brightness}% and {colorTemp} K`, or `The lamp is off` |
| `get_status` | `get_status` with `{}`; the lamp returns its state and ambient light. | The returned state as JSON |
| `set_timer` | `set_timer` with `{ minutes, action: "off" }` | `The lamp turns off in {minutes} minutes` |

## The lamp

The lamp does not use the SDK. Over MQTT, for example, its firmware does this:

```c
mqtt_connect(broker, will = { "lamp/{id}/presence", "offline", retain });
mqtt_publish("lamp/{id}/presence", "online", retain);

on_message("lamp/{id}/cmd", msg) {            // a Client Tool call, forwarded
  switch (msg.command) {
    case "set_light":  apply(msg.args); reply(msg.requestId, ok, state()); break;
    case "get_status": reply(msg.requestId, ok, { ...state(), ambientLux: lux() }); break;
    case "set_timer":  timer_start(msg.args.minutes * 60, led_off); reply(msg.requestId, ok, {}); break;
    default:           reply(msg.requestId, error, "unknown command");
  }
}

on_button_press() { mqtt_publish("lamp/{id}/talk", ""); }

on_message("lamp/{id}/voice_grant", grant) {  // audio goes to TiDB Link directly
  ws = websocket_connect(grant.websocketUrl, grant.connectToken);
  while (ws.open) { ws.send(mic_read(20ms)); if (ws.has_audio()) speaker_play(ws.recv()); }
}
```

## Required tests

Every implementation has tests for these cases, matching the TypeScript tests:

1. An online lamp's Session declares exactly the tools in `tools.json`, starts
   from the stored cursor and forwards a tool call to the lamp; the result is the
   output above. When the lamp drops off the device channel, the next call fails
   with `The lamp is offline`. Going offline detaches the Session.
2. Typing while the lamp is offline attaches, sends and detaches a temporary
   connection and returns the answer. Asking to talk while offline fails; once
   online, the lamp receives the grant on `voice_grant`.
3. A lamp whose connection another process took over is no longer online here.
   Bringing an unbound lamp online fails.
4. Binding creates the lamp's Session from its model's Template with key
   `lamp.{deviceId}` and stores the binding. Changing the model switches the
   Session to that model's Template with key `lamp.{deviceId}.{changeId}` and
   returns the receipt. An unknown model fails before any request.
5. Against the real service: bind a lamp, answer a typed message while offline
   and while online with drafts, hand out a voice grant, go offline, and change
   the lamp's model.
