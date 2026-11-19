# `tidblink-server` (Python)

Official TiDB Link SDK for Python backends, for Python 3.11 or newer, with an
asyncio API and type hints. It uses API Keys that must stay on your backend; do
not ship it to browsers or devices.

> [!NOTE]
> This SDK is in development and not yet published to PyPI. Install it from
> this repository.

It follows the [TypeScript SDK](../typescript/README.md): the same supported
scope, behaviour, errors and recovery rules, for the same exact service
revision ([201c30fb](https://github.com/mem9-ai/agent-stack/commit/201c30fb737c29db929880b2f1f06e54fd247a44)).
The TypeScript README is the reference for what each capability does and how to
recover from an uncertain outcome; this README covers how that surface looks in
Python.

## Supported scope

Organization information, End User provisioning, Template and Skill
management, serving Connectors and reading them, Template/model discovery, Session
management and Template switches, text submission, steering, cancellation,
checkpoint retry, original-result waiting, Client Tools, Session connections
over Backend Connections with progress and events, conversation history, file
attachments, Realtime Model/Connection preparation, prerecorded audio and User
usage reads. Every resource workflow uses an Organization API Key with an
explicitly selected End User.

## Install

```sh
pip install "git+https://github.com/mem9-ai/tidblink-sdk.git#subdirectory=python"
```

Runtime dependencies: `httpx` (HTTP) and `websockets` (Backend Connections).

## Quick start

```python
import asyncio
import os
import uuid

from tidblink_server import OrganizationClient


async def main() -> None:
    async with OrganizationClient(
        base_url=os.environ["AGENT_SERVICE_URL"],
        api_key=os.environ["ORGANIZATION_API_KEY"],
    ) as organization:
        end_user = await organization.create_end_user(external_id="your-application-user-id")
        # Safe to repeat: the same external_id returns the same End User.
        user = organization.user(end_user["userId"])
        page = await user.list_agent_templates()
        if not page["templates"]:
            raise RuntimeError("Select an available Template; follow nextCursor if hasMore")
        # Persist the selected Template and the creation key before calling.
        session = await user.create_session(
            template_id=page["templates"][0]["agentTemplateId"],
            idempotency_key=str(uuid.uuid4()),
        )
        # Retain the Session, command identity and original input before dispatch.
        client_event_id = str(uuid.uuid4())
        receipt = await session.submit_message(client_event_id=client_event_id, text="Help me plan a trip")
        result = await session.wait_for_result(receipt["inputId"])
        print(result.text)
        # Waiting again reads the same original result; it executes nothing.
        recovered = await session.wait_for_result(client_event_id)


asyncio.run(main())
```

## How the TypeScript API maps to Python

| TypeScript | Python |
| --- | --- |
| `session.submitMessage({ clientEventId, text })` | `await session.submit_message(client_event_id=..., text=...)` |
| Option objects (`{ limit, cursor, requestId }`) | Keyword-only arguments (`limit=`, `cursor=`, `request_id=`); an unknown one is a `TypeError` |
| An omitted field vs `null` | `OMIT` (the default where `None` is meaningful) vs `None`, e.g. `billing_tag=None` clears a tag |
| Records, receipts, events, Template content, tool schemas | The service's JSON, unchanged: `dict`s with the service's camelCase keys, typed as `TypedDict`s (`session.data["template"]["contentDigest"]`) |
| Results the SDK builds (`TurnResult`, `TurnState`, `SessionPage`, `AudioTurnResult`, connection ends, call contexts, `FileBytes`/`FileDownload`) | Frozen dataclasses with snake_case fields (`result.turn_id`, `page.next_cursor`) |
| `timeoutMs`, `pollIntervalMs` | `timeout`, `poll_interval`, in seconds |
| `AbortSignal` | Cancel the task: `CancelledError` propagates unchanged; cancelling after dispatch does not establish whether a mutation happened |
| `fetch` option | `http_client=httpx.AsyncClient(...)`; it carries every HTTP request, including file transfers to presigned URLs, which never receive the API Key |
| `WebSocket` option | `websocket_connect=`, replacing how Backend Connections are opened |
| `Uint8Array` bytes | `data=` as `bytes`, `bytearray` or `memoryview`, hashed and sent in place without copying the whole buffer |
| `ConnectionError` | `TiDBLinkConnectionError`, so that it does not shadow Python's builtin |
| `TypeError` for invalid input | `TypeError` for a wrong type, `ValueError` for a wrong value, raised before any request |
| `organization.close()` | `await organization.close()` or `async with OrganizationClient(...)`; it also closes the HTTP client the SDK created (one passed as `http_client` stays open), so REST calls stop afterwards |

Error classes keep their TypeScript names otherwise (`TiDBLinkApiError`,
`ConflictError`, `OutcomeUnknownError`, `TurnWaitTimeoutError`,
`InputNotAppliedError`, `TurnFailedError`, `TurnInterruptedError`,
`BackendConnectionClosedError`, `BackendRequestError`, `FileUploadError`, …),
with snake_case attributes (`error.request_id`, `error.input_id`). The error an
SDK error was raised from is its `__cause__`, also available as `error.cause`.

## Templates and Sessions

```python
customer = organization.user(os.environ["END_USER_ID"])
models = await customer.list_models()
session = await customer.create_session(
    template_id=template_id,
    idempotency_key="customer-conversation-1",
    model=models["effectiveDefaultModel"],
    billing_tag={"key": "account", "value": "customer-1"},
    additional_instructions="Prefer concise answers.",
    sandbox={"workingDirectory": "projects/trip"},
)
await session.rename("Planning")
await session.set_billing_tag(None)
await session.set_model(models["effectiveDefaultModel"])  # uses the handle's model ETag

receipt = await session.switch_template(template_id=next_template_id, idempotency_key=switch_key)
if receipt["status"] == "pending":
    handoff = await session.get_turn(receipt["handoffTurnId"])
await session.refresh()
```

`list_sessions()` returns a `SessionPage` of `Session` handles;
`customer.session(id)` makes a handle without a request and
`customer.get_session(id)` reads it.

## Text messages, control and history

```python
result = await session.turn(client_event_id=saved_id, text="Plan the trip", timeout=60)
original = await session.reconcile_interaction(saved_id)  # None if no receipt is visible yet
stop = await session.cancel(client_event_id=cancel_id, turn_id=result.turn_id)
state = await session.get_turn_state(result.turn_id)       # TurnState(turn, stop_requested_at)
retry = await session.retry(client_event_id=retry_id, turn_id=result.turn_id)
turns = await session.history(limit=20)
timeline = await session.timeline(limit=20)
```

## Connecting a Session

`session.connect()` attaches a Session to this process's Backend Connection. It
receives the Session's events and declares and executes its Client Tools, with
the reconnection, claim-before-run and deadline rules of the
[TypeScript SDK](../typescript/README.md#connecting-a-session).

```python
from tidblink_server import ClientToolHandler


async def set_light(args, context):
    state = await ask_device(device_id, "set_light", args)  # cancelled at context.deadline_at
    return f"On at {state['brightness']}%" if state["on"] else "Off"


conn = await organization.user(user_id).session(session_id).connect(
    tools={
        "set_light": ClientToolHandler(
            description="Turn the lamp on or off, or set its brightness",
            parameters={"type": "object", "properties": {"on": {"type": "boolean"}}},
            run=set_light,
        )
    },
    cursor=await db.get_cursor(device_id),
    on_cursor=lambda cursor: db.save_cursor(device_id, cursor),
    on_event=forward,
)
reply = await conn.send("Turn the light on", on_progress=lambda draft: show(draft))
grant = await conn.realtime_grant()
await conn.close()
end = await conn.closed  # SessionConnectionEnd(reason="closed")
```

`conn.send()` raises the same errors as `session.turn()`. Once the message is
sent, its `timeout` never raises a plain `TimeoutError`, since the core may
already have the message: it raises `TurnWaitTimeoutError` with `input_id`;
recover with `session.wait_for_result(input_id)` or send again with the same
`client_event_id`.

A Client Tool call made while no connection executes the Session is reported to
the model as not performed at once: no `run` handler sees it, and no
`requires_action` event or audio `client_tool_calls` frame records it. Calls
are executed only by the Session's executing connection; no REST command
answers them.

Callbacks may be plain functions or coroutines. They run one event at a time;
an exception in one goes to `on_error`, or else to the event loop's exception
handler. At a Client Tool call's deadline the SDK cancels the `run` task and
reports the timeout.

The smart lamp example, [`examples/lamp_backend.py`](examples/lamp_backend.py),
follows the [shared specification](../examples/lamp/README.md).

## Files, audio, Realtime and usage

```python
file = await customer.files.upload(
    original_name="notes.txt",
    data=Path("notes.txt").read_bytes(),
    content_type="text/plain",
    idempotency_key=saved_upload_key,
)
content = await customer.files.download(file["fileId"])

audio = await session.audio(Path("recording.wav").read_bytes(), filename="recording.wav", content_type="audio/wav")

prepared = await session.open_realtime_connection(metadata={"terminal": "kiosk-1"})

usage = await organization.get_user_usage(
    user_id, from_="2030-01-02T00:00:00.000Z", to="2030-01-03T00:00:00.000Z", interval="day"
)
```

`session.stream_audio(...)` yields the audio Turn's events. Leaving it early
should close it (`await stream.aclose()` or `contextlib.aclosing`), which may
request interruption of the Turn, as breaking the loop does in TypeScript.

## Management and Connectors

```python
catalog = await organization.templates.catalog()
created = await organization.templates.create(display_name="Support assistant", content=content, idempotency_key=key)
upload = await organization.skills.upload(data=zip_bytes, idempotency_key=upload_key)
connectors = await organization.connectors.list()
```

A Connector is a **cloud capability**: functions your backend provides to the
Sessions of every End User, such as an order lookup or a knowledge base. (Device
functions are [Client Tools](#connecting-a-session) instead.) Your backend
serves a Connector over the same Backend Connections that carry Session
connections, so it needs no public endpoint, no callback verification and no
separate registration:

```python
from tidblink_server import ClientToolHandler


async def get_order(args, context):
    # Answer only with the data of the End User whose Session made the call.
    return json.dumps(await db.orders.find(context.user_id, args["orderId"]))  # cancelled at context.deadline_at


orders = await organization.serve_connector(
    name="orders",
    tools={
        "get_order": ClientToolHandler(
            description="Read one of the current user's orders",
            parameters={
                "type": "object",
                "properties": {"orderId": {"type": "string"}},
                "required": ["orderId"],
            },
            run=get_order,
        )
    },
    on_error=lambda error: log.warning("Connector error: %s", error),
)
orders.connector_id  # reference it from a Template: content["tools"] = {"mcp": [{"serverId": ...}]}
await orders.close()  # stop serving, for example on shutdown
end = await orders.closed  # ServedConnectorEnd(reason="closed")
```

- **Serving is registration.** The first `serve_connector` with a name creates
  the Organization's Connector with that name; later ones, from any process or
  Key of the Organization, serve the same Connector and answer the same
  `connector_id`. The latest declaration served is what new Turns use.
  `organization.connectors.list()` and `get(connector_id)` read the Connectors
  declared this way, whether or not a process serves them now, as
  `{connectorId, displayName, createdAt, updatedAt}`; there is nothing to
  activate or edit. A Connector left from the retired HTTP callback or tunnel
  transports is not listed until a process serves its name, which takes it over
  with the same `connectorId`, so Templates referencing it need no change.
- **Names.** The Connector name and each function name are lowercase letters
  and digits joined by single underscores, 1 to 29 characters. The model sees
  each function as `mcp_<name>__<function>`, for example `mcp_orders__get_order`.
  At most 64 functions; `parameters` is a JSON Schema with `"type": "object"`
  that may not define or require `async`, which the platform reserves. Invalid
  arguments raise `TypeError` or `ValueError` before anything is sent. Serving a
  name this client already serves raises `TiDBLinkError`; close the first one.
- **Handlers.** `tools` is written exactly like `session.connect(tools=...)`:
  `ClientToolHandler`s or mappings with `description`, `parameters` and `run`.
  `run(args, context)` receives the `ClientToolCallContext` of a Client Tool
  (`call_id`, `session_id`, `user_id`, `turn_id`, `name`, `deadline_at`) whose
  `connector` is a `ToolCallConnector(id, name)`; on a Client Tool call it is
  `None`. Calls come from any Session whose Template references the Connector,
  of any End User; `user_id` names the Session's owner, so act for that user
  rather than trusting model arguments. Return text, or a value sent as JSON;
  raise to report a failure, whose message the model sees.
- **Claim and deadline.** The core sends each call to one connection serving
  the Connector, preferring the one executing the calling Session. The SDK
  claims it there and runs the handler only once the claim is granted, at most
  once per `call_id`, and submits the result over any connection. At
  `deadline_at`, 60 seconds after the call, the SDK cancels the `run` task, and
  the model is then told the result is unknown.
- **Availability.** Serving keeps at least one Backend Connection open, declares
  the Connector on every connection the client opens, and declares it again
  after a reconnect or `connection.drain`. When no process serves the
  Connector, its calls end at once as not performed and the Turn continues: the
  model is told the function did not run. `closed` resolves with
  `ServedConnectorEnd(reason="closed")` after `close()` or
  `organization.close()`, or `ServedConnectorEnd(reason="error", error=...)`
  when no connection can serve it any more, for example after the Key was
  revoked. `on_error` receives errors that do not end serving (a declaration a
  connection refused, an invalid call, a result the core did not accept);
  without it they go to the event loop's exception handler.

[`examples/serve_connector.py`](examples/serve_connector.py) serves an `orders`
Connector from your own order data.

## Examples

[`examples/`](examples) holds the scenarios indexed in
[`../examples/README.md`](../examples/README.md). Each exports an async workflow
function and runs as a script with the same environment variables as its
TypeScript counterpart, for example:

```sh
AGENT_SERVICE_URL=... ORGANIZATION_API_KEY=... END_USER_ID=... SESSION_ID=... \
CLIENT_EVENT_ID=... MESSAGE_TEXT=... python examples/send_message.py
```

## Development

```sh
cd python
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The tests port the TypeScript SDK's simulated-service tests, using
`httpx.MockTransport`, local HTTP servers and a fake core for Backend
Connections. Not yet ported: the frozen service-contract checks and the
actual-service suite (`npm run test:service`), so this SDK has no
actual-service evidence yet.

`TIDB_LINK_API_VERSION`, `TIDB_LINK_REVISION` and `TIDB_LINK_OPENAPI_SHA256`
identify the service baseline; a test keeps them equal to the TypeScript
package's.
