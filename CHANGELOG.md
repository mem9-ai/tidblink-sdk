# Changelog

## Unreleased

## 0.6.0-rc.0

Prerelease on the `next` dist-tag. It breaks 0.5: see the Session language
rename below.

- Align with service
  [c6ae2e37](https://github.com/mem9-ai/agent-stack/commit/c6ae2e37c604368ed8dc365a0bc73ea3387f3bd7),
  OpenAPI SHA-256
  `f3ed7342d8156d191d9756d99329d62ef549d05b369cca27c938f0eb973f3269`, which
  adds File Notifications and reads of files in a Session's Drive, and
  renames the Session's language field (below). The
  frozen files subset adds the Organization routes
  `sessions/{id}/files/content` and `sessions/{id}/files/download-tickets`.
  Otherwise the subsets change only by the error codes
  `drive_file_version_conflict` and `drive_file_too_large`, by the optional
  `file` of an `operation_step` event, by the optional `fileRefusal` of a
  failed `operation_step` event and of an Operation in Turn reads, and by
  descriptions.
  The management subset adds the required `etag` of an Organization Skill
  read, for the service's new-Version upload route, which this SDK does not
  call yet; `SkillSummary` does not expose it.
  `drive_file_too_large` is the Console's refusal to download a Template
  Test's large file; no route the SDK calls answers it.
- **Breaking:** Sessions carry their fixed Realtime Language as
  `realtimeLanguage: "zh-CN" | "en-US" | null` instead of
  `realtimeVoice: { language } | null`, following the service rename (ADR 0117
  reserves `realtimeVoice` for the voice selection). `RealtimeModelSelection`
  from `getRealtimeModelSelection` and `setRealtimeModel` replaces
  `voice: { language } | null` with `language: RealtimeLanguage | null`, and
  `RealtimeVoiceSetting` is no longer exported. A `setRealtimeModel` receipt
  without a valid `realtimeLanguage` raises `OutcomeUnknownError`. The Python
  SDK makes the same change.
- `session.connect({ onFile })` runs for each file the Agent announces with a
  File Notification: an `agent.session.turn.item.added` `operation_step` whose
  payload carries `file`, `{ path, name, version, byteSize, updatedAt }`
  (`NotifiedFile`). It runs after `onEvent`, which still receives the raw
  event, and before the cursor advances. `asKnownSessionEvent` accepts the
  optional `file` and does not narrow an event whose `file` is malformed.
- `session.files` (`SessionDriveFiles`) reads files in the Session's Drive.
  `read(path, { version })` returns the bytes and, for a file above 50 MiB,
  follows the download ticket the service answers with, without the API Key.
  `createDownloadTicket(path, { version })` returns `{ url, expiresAt,
  byteSize }` with an absolute `url` that a device downloads without a key.
  The `version` is opaque: the SDK sends any non-empty text as announced and
  leaves its form to the service.
- `DriveFileVersionConflictError`, a `ConflictError`, is raised when a pinned
  read, ticket or ticket download finds that the file has changed
  (`drive_file_version_conflict`). A missing file stays a 404
  `TiDBLinkApiError` with code `drive_file_not_found`, and a missing Session
  one with code `session_not_found`.
- Turn reads type the `file.notify` Operation kind and its
  `file_notification` result pointer. The audio Turn's `operation_step` event
  type carries the same optional `file`.
- A File Notification the Agent could not make says why: its failed
  `operation_step` and the failed `file.notify` Operation in Turn reads carry
  `fileRefusal`, `{ reason, path? }` (`FileNotificationRefusal`), with a
  `reason` such as `not_found`, `directory` or `local_only` (a folder such as
  `build/` that the Sandbox never saves) and the path the Agent tried. The
  service may add reasons. `asKnownSessionEvent` (`as_known_session_event`)
  accepts the optional `fileRefusal` and does not narrow an event whose
  `fileRefusal` is malformed; `onFile` does not run for it. The audio Turn's
  `operation_step` event type carries the same optional `fileRefusal`.
- The packaged `flash-device.mjs` example (`examples/flash_device.py` in
  Python) is the notify-then-Client-Tool device scenario: its `flash_device`
  Client Tool hands the device a keyless ticket for the announced version, and
  the device's answer is the tool's result. The service verification runs it.
  `flashDeviceWorkflow` (`flash_device_workflow`) sends the message and keeps
  the device connected until the Turn ends, waiting up to 10 minutes by
  default because a build can take minutes; the script reads the wait from
  `TURN_TIMEOUT_SECONDS`. The READMEs say to build files the Agent hands over
  into a synced folder such as `out/`, since `build/`, `dist/`, `target/` and
  `tmp/` stay in the Sandbox.
- The Python SDK offers the same: `session.connect(on_file=...)`,
  `session.files.read(path, version=...)`,
  `session.files.create_download_ticket(path, version=...)`,
  `DriveFileVersionConflictError` and `NotifiedFile`.
- The methods are on the Organization-scoped Session handle,
  `organization.user(userId).session(sessionId)`; the SDKs still support no
  User API Keys.

## 0.5.1

Stable release on the `latest` dist-tag for service
[3e920bc7](https://github.com/mem9-ai/agent-stack/commit/3e920bc73c754dce4cbd5782811063da770be290),
OpenAPI SHA-256
`cfa915a6e1c143c00abf3a3554c4da805132742114d9125992d270551875185c`, which is
deployed to production. It only adds to 0.5.0.

- Align with service `3e920bc7` instead of `201c30fb`. The frozen OpenAPI
  subsets change only by the error codes `agent_not_found` and
  `agent_preview_not_enabled`; the Backend Connection protocol is unchanged.

### Gated features

Features TiDB Link enables only for some Organizations, from the new entry
`@tidblink/server/gated`. They may change or be removed in any release; see
[RELEASING.md](RELEASING.md#versions-and-channels).

- `agents(user)` manages an End User's Agents, named presets of Session
  creation settings: `create`, `list`, `get`, `update`, `delete`,
  `createSession`, which returns an ordinary `Session`, and `listSessions`.
- `FeatureNotEnabledError`, a `TiDBLinkApiError` with `feature`, is what every
  call returns for an Organization that is not enabled for the feature.
- The Python SDK offers the same from `tidblink_server.gated`, with
  `agents(user).create(name=..., session_defaults=..., idempotency_key=...)`
  and the other methods in snake case.

## 0.5.0

Stable release on the `latest` dist-tag for service
[201c30fb](https://github.com/mem9-ai/agent-stack/commit/201c30fb737c29db929880b2f1f06e54fd247a44),
OpenAPI SHA-256
`74215cefcabb18e915fa6a13d4a4dbcbd3720aa1eb5b522e0fd3e83d2b6ad131`, which is
deployed to production. It contains every change in 0.5.0-rc.0 and
0.5.0-rc.1 below; compared with 0.4.0, Connectors are served from the
application backend with `organization.serveConnector()` over Backend
Connections, HTTP callback Connectors and Connector registration are removed,
a `Connector` record only names the declared Connector, and `conn.send()`
reports an unanswered message stopped by its deadline or signal as an unknown
outcome. Since 0.5.0-rc.1:

- Pin service `201c30fb`, the production deployment, instead of `07d1f9c8`,
  which only adds documentation; the OpenAPI and Backend Connection contracts
  are identical.

## 0.5.0-rc.1

Prerelease on the `next` dist-tag.

- Align with service
  [07d1f9c8](https://github.com/mem9-ai/agent-stack/commit/07d1f9c85c0fedd729c76c4cb4a48485d139c8ff),
  OpenAPI SHA-256
  `74215cefcabb18e915fa6a13d4a4dbcbd3720aa1eb5b522e0fd3e83d2b6ad131`, which
  removes the HTTP callback and tunnel Connector transports and their
  management routes.
- **Breaking:** a `Connector` record is `{ connectorId, displayName, createdAt,
  updatedAt }`: `organizationId`, `endpointUrl`, `endpointVersion`,
  `transport`, `status`, `connectorVersion` and `observation` are removed, and
  so is the `ConnectorTransport` type. `connectors.list()` and `get()` return
  only Connectors declared with `serveConnector`; one left from the retired
  transports appears again once a process serves its name, with the same
  `connectorId`.
- `TurnFailure.details.registrations[].reason` adds `catalog_unavailable`: the
  MCP Server Registration's authorization is usable but its tool catalog has
  not been discovered.
- The README describes the lamp example as it is since 0.4.0: `templates`,
  `bindDevice(appUserId, deviceId, model)` and `changeModel`.

## 0.5.0-rc.0

Prerelease on the `next` dist-tag. Aligns with service
[501eee1c](https://github.com/mem9-ai/agent-stack/commit/501eee1c608111c1f04ac9311b9c55ed90be8170),
OpenAPI SHA-256
`29f5ea39cc310ce69ece6b2351fac64bba015c07e83dd11a532be3e073feb96b`, where
Connectors are served over Backend Connections (ADR 0109).

### Connectors

- Add `organization.serveConnector({ name, tools, onError? })`. It serves a
  Connector's functions from this process over its Backend Connections and
  resolves with a `ServedConnector`: `connectorId` (reference it from a
  Template's `tools.mcp[].serverId`), `closed` and `close()`. Serving a name
  creates the Organization's Connector with that name; there is no separate
  registration or activation. `tools` is written like `session.connect({ tools
  })`, and a handler's context also carries `connector: { id, name }`. The
  model sees each function as `mcp_<name>__<function>`. Calls are claimed on
  the connection that delivered them, run at most once per `callId`, abort at
  their deadline, and can come from any Session of the Organization. The client
  declares a served Connector on every Backend Connection, declares it again
  after a reconnect or `connection.drain`, and keeps one connection open while
  any Connector is served. Names and function declarations are checked locally
  and throw a `TypeError` before anything is sent.
- **Breaking:** remove HTTP callback Connectors: `verifyConnectorCallback`,
  `readConnectorCallContext`, `ConnectorKeySet`, `ConnectorAuthError`,
  `CONNECTOR_CALLBACK_ISSUER`, `CONNECTOR_CALLBACK_AUDIENCE`,
  `CONNECTOR_CALL_CONTEXT_KEY` and their types, and the `http-connector.mjs`
  example, replaced by `serve-connector.mjs`.
- **Breaking:** `organization.connectors` only reads: `register`, `update`,
  `activate` and `deactivate` are removed, with `RegisterConnectorInput`,
  `UpdateConnectorInput` and `ConnectorVersionOptions`. `Connector.transport`
  adds `backend_connection`, and `observation` is always present, null for
  Connectors that are not `streamable_http`.

### Session connections

- **Breaking:** `conn.send()` stopped by its `timeoutMs` or `signal` while the
  core had not yet answered the message now raises `TurnWaitTimeoutError` or
  `OutcomeUnknownError` with `sessionId` and `inputId`, like `session.turn()`,
  instead of the signal's `TimeoutError` or `AbortError`: the core may already
  have accepted the message. Recover it with `session.waitForResult(inputId)`
  or send again with the same `clientEventId`. The Python SDK's `conn.send()`
  raises `TurnWaitTimeoutError` instead of `TimeoutError` in the same case.

### Service alignment

- A request refused with `authentication_required` now closes its Backend
  Connection with 4001 at once. The request fails with that refusal and every
  Session connection and served Connector of the connection ends with reason
  `error`; nothing reconnects with the invalid Key.
- `UserFile.source` is `{ kind: "user_upload" }` or, for a file a Session
  produced, `{ kind: "session", sessionId, operationId }`; the
  `artifact_revision` form, which the service no longer returns, is removed.
- `TurnFailure.details.registrations` lists the MCP Server Registrations whose
  authorization is missing when a Turn fails with `mcp_authorization_required`.
  A message refused for that reason is not saved.
- Template content's `tools.mcp[]` entries accept `toolNames`, which limits the
  tools an MCP Server Registration exposes.

## 0.4.0

Stable release on the `latest` dist-tag for service
[c21a5e3a](https://github.com/mem9-ai/agent-stack/commit/c21a5e3a091c938576ee3bb36cba72baf0e87022),
OpenAPI SHA-256
`0b7b19720f557a0b3385b297cf59207e94d6c087100192d4f65800642dc491ba`. It
contains every change in 0.4.0-rc.0, 0.4.0-rc.1 and 0.4.0-rc.2 below; compared
with 0.2.0, the previous stable release, the package is now `@tidblink/server`
for Organization API Keys, creates Sessions directly from a Template and
connects Sessions to this process over Backend Connections. Since 0.4.0-rc.2:

- **Breaking:** the `lamp-backend.mjs` example uses one Template per lamp model.
  `createLampBackend` takes `templates`, a map from lamp model to Template ID,
  instead of `templateId`, and `bindDevice(appUserId, deviceId, model)` creates
  the lamp's Session from its model's Template. The new
  `changeModel(deviceId, model, { changeId? })` moves the Session to another
  model's Template with `session.switchTemplate()`, keeping its history.

## 0.4.0-rc.2

- Align with service
  [c21a5e3a](https://github.com/mem9-ai/agent-stack/commit/c21a5e3a091c938576ee3bb36cba72baf0e87022),
  which removes the per-Session event subscription and answering Client Tool
  calls over REST (`agent.session.input.client_tool_output`). The SDK used
  neither. A Client Tool call made while no Session connection executes the
  Session is now reported to the model as not performed at once, instead of
  waiting out its 60-second deadline; it reaches no connection, so no `run`
  handler sees it, and no `requires_action` event or audio `client_tool_calls`
  frame records it. Every new call carries `claimBy`.
- `files.upload()`, `uploadContent()` and `uploadPart()` hash and send the
  caller's bytes in place instead of copying them: the SDK and Node.js fetch
  used to hold about three more copies of a file while it uploaded. Leave the
  bytes unchanged until the call settles. The SDK now hashes with
  `node:crypto`, because Web Crypto's digest also copies its input, and hashes
  each file and part once instead of two or three times.
- With the default fetch, an upload body is now a stream of the caller's
  array, sent with `duplex: "half"` and an explicit `content-length`, because
  Node.js's fetch copies an array body. Such a body can be sent only once, so
  the SDK can never retry that request; upload byte transfers were already
  never retried automatically, so no request loses a retry. A custom `fetch`
  still receives the caller's `Uint8Array` as the body, which wrappers that
  rebuild or read the request, and fetch implementations such as node-fetch,
  can handle; whether it copies the bytes is up to that fetch.
- With the default fetch, an upload byte transfer that failed before fetch
  read any of its body, such as a refused connection or an abort, now fails
  with `ConnectionError`, the abort or the local error instead of
  `OutcomeUnknownError`: it sent no body bytes, so the service cannot have
  received it. `upload()` reports such a failure, and bytes whose length
  changed after hashing, as a new `FileUploadError` that carries `uploadId`,
  `idempotencyKey` and the original error as `cause`, instead of
  `FileUploadOutcomeUnknownError`. With a custom `fetch` the SDK cannot tell
  what was sent, so such a failure is still an unknown outcome.
- `uploadContent()` and `uploadPart()` reject bytes that are not a
  `Uint8Array` with a `TypeError`, as `upload()` already did. All three reject
  bytes in a `SharedArrayBuffer`, and bytes whose length changed after they
  were hashed, with a `TypeError` before sending them.
- Add `session.getTurnState(turnId)`, which returns the Turn with
  `stopRequestedAt`: when a request to stop its current execution was
  accepted, or null. A Turn can still be running after a stop request;
  `getTurn()` alone did not show one.
- Breaking for TypeScript: `TemplateContent.realtime` is now
  `{ language } | null`, because the service accepts and returns `null` for a
  Template without voice. Code that reads `content.realtime.language` must
  handle `null` first.

## 0.4.0-rc.1

- Observing Session connections bound the events waiting for their
  callbacks. The SDK keeps reading the core while `onEvent` and the other
  callbacks run, so slow callbacks used to queue every later event in memory.
  Past `maxBufferedBytes` (a new `connect()` option for observing connections,
  default 8 MiB) an observing connection now drops events and, once the
  callbacks have caught up, attaches again from the last event they completed,
  so the core sends the rest again in order. Executing connections still queue
  every event.
- Fix events lost right after an attach. Node.js's WebSocket hands over
  messages that arrive together in one task, so events the core sent with the
  `session.attach` answer, such as the replay after a `cursor`, reached the SDK
  before it had recorded the attachment, and were dropped. Such events now
  reach the callbacks once `connect()` has returned.

## 0.4.0-rc.0

Release candidate on the `next` dist-tag for service `4305ef67`. Compared with
0.3.0-rc.0 it removes User API Key authority, Schedulers, every Agent API and
the per-Session event subscription, creates Sessions directly from a Template,
and connects Sessions to this process over Backend Connections (service
ADR 0109).

- Pin the supported contract to service
  `4305ef67daebf23c416badf806217a7c11dcef37` and OpenAPI SHA-256
  `6f5ff6f025eef80fbd9d5b80fb798e5db159aaa006e7d34866e9c3b6843dfe58`. Since
  0.3.0-rc.0 the service retired native Hardware, so it no longer lists
  Hardware error codes or Session `hardwareIds`, and it no longer answers 429
  `llm_spend_limit_exhausted`.

### Backend Connections

- Add `session.connect(options)`, which attaches a Session to this process's
  Backend Connection and returns a `SessionConnection`. Callbacks `onEvent`,
  `onTranscript`, `onResponse` and `onCursor` receive the Session's events in
  order; `cursor` resumes after a saved event. The SDK keeps one WebSocket per
  `maxSessions` attachments, re-attaches after a lost or draining connection
  without gaps or repeats, probes liveness with `ping`, and closes idle
  connections. `observe: true` attaches for events only. `closed` resolves
  with why the connection ended (`closed`, `superseded`, `session_deleted` or
  `error`); `close()` releases the Session and `OrganizationClient.close()`
  closes every connection. `OrganizationClient` accepts a `WebSocket`
  constructor.
- Session connections execute Client Tools: `connect({ tools })` takes a
  handler with `description`, `parameters` and `run(args, context)` per tool
  name. The SDK claims each call and runs it only once the core grants the
  claim, submits the returned value or thrown error as the result over any
  Backend Connection until the call's deadline, runs each `callId` at most
  once, and aborts `context.signal` at `deadlineAt`. A refused result reaches
  `onError` as `ClientToolResultRefusedError`.
- Add `conn.send(text, options)`: it sends a user message over the Backend
  Connection (`session.send`) and resolves with the `TurnResult` of the Turn it
  starts or steers, after the Session releases that Turn, so a follow-up
  message, audio Turn or Template switch is not refused as busy. `onProgress`
  receives the complete draft from pushed events; the settled result is read
  once over REST. A message whose answer a reconnect lost is sent again with
  the same `clientEventId`. Refusals raise `BackendRequestError` with
  `details`.
- Add `conn.realtimeGrant({ audio?, metadata? })`, which returns the WebSocket
  URL and connect token a device uses to join the Session's Realtime
  connection, issuing a new token when one is already live.
- Add typed Session events (`KnownSessionEvent` and one type per event,
  including `SessionIdleEvent` for `agent.session.idle`) and
  `asKnownSessionEvent`. Unknown events are still delivered unchanged. A
  Turn's result is visible before the Session releases it: callers of
  `session.turn()` or `session.audio()` that start the next Turn or switch the
  Template right away should wait for `agent.session.idle`, or for
  `session.data.activeTurnId` to become `null`.
- New exports: `SessionConnection`, `ConnectOptions`, `SendOptions`,
  `ClientToolHandler`, `ClientToolHandlers`, `ClientToolCallContext`,
  `RealtimeTranscript`, `RealtimeResponse`, `SessionConnectionEnd`,
  `BackendWebSocket`, `WebSocketConstructor`, `BackendConnectionClosedError`,
  `BackendRequestError` and `ClientToolResultRefusedError`.
- **Breaking:** remove the APIs built on the per-Session event subscription,
  which Backend Connections replace: `session.events()`, `session.observe()`,
  `session.listPendingClientToolCalls()`, `session.submitClientToolOutput()`,
  `ObservationStoppedError` and the types `SessionEventsOptions`,
  `ObserveTurnOptions`, `TurnObservation`, `PendingClientToolCall` and
  `ClientToolOutputInput`. Follow events with `session.connect({ onEvent })`,
  report progress with `conn.send(text, { onProgress })`, and run Client Tools
  with `connect({ tools })`. `submitMessage()`, `waitForResult()` and `turn()`
  remain over HTTP.
- Replace the `observe-message.mjs` and `answer-client-tool.mjs` examples with
  `lamp-backend.mjs`: the backend of a smart lamp that users talk to by typing
  or by voice, keeping the lamp's Session connected while it is online,
  forwarding its Client Tools to the lamp and handing the lamp Realtime
  grants. The README shows its MQTT wiring and the lamp's firmware in
  pseudo-code.

### Templates, Sessions and Agents

- **Breaking:** remove the Agent API, because the service no longer has Agents:
  the `Agent` class, `AgentRecord`, `CreateAgentInput`, `UpdateAgentInput`,
  `EndUserClient.createAgent`, `listAgents` and `getAgent`, and
  `OrganizationClient.unlinkAgent`. `listModels()` no longer accepts `agentId`.
- **Breaking:** create Sessions with `user.createSession({ templateId,
  idempotencyKey, ... })` instead of `agent.createSession(...)`. `templateId` is
  required; there is no default Template. The Agent preferences become Session
  creation options: `additionalInstructions`, `sandbox` (`profileId`,
  `workingDirectory`, typed as `SessionSandboxSelection`) and
  `acknowledgeSessionRecall`.
- **Breaking:** `SessionRecord.createdWithAgentId`, `RealtimeConnection.agentId`,
  the usage item `agentId`, the attribution counter `agentUsageCount` and the
  attribution kind `agent` are gone. A Realtime Connection without `agentId` is
  now accepted; the previous version rejected every receipt from this service.
- Add `session.switchTemplate({ templateId, idempotencyKey,
  acknowledgeSessionRecall? })` to move an idle Session to another Template
  ([#28](https://github.com/mem9-ai/tidblink-sdk/issues/28)). It returns the
  service's receipt, typed as `TemplateSwitchReceipt` with one variant per
  `status`: `unchanged`, `succeeded`, or `pending` with the `handoffTurnId` of
  the Template Handoff Turn; a later read can show `failed` with `error.code`.
  Follow a pending switch through its Handoff Turn, the
  `agent.session.template_switch.*` events or `refresh()`; the call does not
  update `session.data`. After a lost response, repeat the same key and input
  to recover the receipt. An uncertain outcome raises `OutcomeUnknownError` with
  `sessionId`; the 503 refusals `turn_admission_closed` and
  `skill_package_store_unavailable` are definite `TiDBLinkApiError`s.
  `SwitchTemplateInput`, `TemplateSwitchReceipt` and `TemplateSwitchStatus` are
  exported.
- `listSessions({ templateId })` lists the Sessions currently on a Template,
  and `getUserUsage` / `getUserUsageDetails` accept `templateId`; both send it as
  `agentTemplateId`. `SessionListOptions` is exported.
- Turns from history and the timeline carry `agentTemplateId`, the Template
  admitted for the Turn. A Template switch adds a `TemplateHandoffTurnRecord`
  (`kind: "template_handoff"`) with no `userMessage`; narrow on `kind` before
  reading `userMessage`.
- Usage items carry `agentTemplateId`, and usage totals may be in `CNY`.
  Template content types `tools.mcp`, the MCP Servers and Connectors a Template
  uses.
- The `create-session.mjs` example takes `templateId` and one
  `sessionCreationKey`; `AGENT_CREATION_KEY` is gone. It passes
  `additionalInstructions`, `sandbox` and `acknowledgeSessionRecall` through,
  and its CLI accepts `ACKNOWLEDGE_SESSION_RECALL=true`.
- `createSession` and the `templateId` filters reject a Template ID that is
  empty, longer than 64 characters or padded with whitespace before sending.
- A Hardware Operation `source` in history is typed with `hardwareBindingId`,
  or with `hardwareRequirementId` for historical calls.
- New service fields are typed: `SessionRecord.configurationEpoch` and
  `switching`, the `template_switch` timeline entry (`SessionTemplateSwitch`),
  and `realtimeConnectionLive` on Realtime usage items.

### Organization resources

- Add `organization.templates` to create, read, list, replace and archive
  Organization Templates, and to read the reference catalog of models, Sandbox
  profiles and Skills they may use.
- Add `organization.skills` to upload a Skill ZIP, wait for validation, publish
  or cancel it, and read published Skills and their versions.
- Add `organization.connectors` to register, list, read, edit, activate and
  deactivate Connectors.
- Add `verifyConnectorCallback` with a cached `ConnectorKeySet` to verify the
  platform's signed requests to an HTTP Connector, and `readConnectorCallContext`
  with the `ConnectorCallContext` type for the End User and Session of a tool
  call. Add the `http-connector.mjs` example.
- `createEndUser` with an `externalId` is idempotent: the service returns the
  existing End User unchanged, and the SDK retries the call automatically.
  It previously failed with `resource_conflict`; that error now means only an
  `externalId` that differs from an existing one by case or accents.

### Authority and client

- **Breaking:** remove User API Key authority. `UserClient`,
  `createUserApiKey`, `listUserApiKeys`, `rotateUserApiKey`,
  `revokeUserApiKey`, the User-Key usage reads (`getUsage`, `getUsageDetails`)
  and their types are gone. Use
  `new OrganizationClient({ baseUrl, apiKey }).user(userId)` for resources and
  `organization.getUserUsage(userId, options)` for usage.
- **Breaking:** remove Scheduler support: `user.schedulers`, its types and the
  `schedule-task.mjs` example. Schedulers are leaving the core platform; call
  the HTTP API directly if you still need them.
- `OrganizationClient` accepts a `fetch` option. Every HTTP request the client
  makes goes through it, including file transfers; the default is the global
  `fetch`. `FetchFunction` is exported.
- Every packaged example now takes `organizationApiKey` and `userId`
  (`ORGANIZATION_API_KEY` and `END_USER_ID` on the command line). Remove the
  `provision-user.mjs` example and fold `organization-session.mjs` into
  `create-session.mjs`; `read-usage.mjs` exports only `readUserUsage`.
- Contract snapshots are regenerated by `scripts/refresh-contract.mjs` and cover
  only the Organization routes the SDK calls.
- Internal: request paths are written relative to the Organization or End User
  scope instead of being rewritten from User-Key routes. Requests on the wire are
  unchanged.
- Release only from release branches. Merging `main` into `release` submits the
  package version to npm; commits on `main` release nothing.

## 0.3.0-rc.0

Release candidate on the `next` dist-tag. It verifies the release process and
gives early adopters the Organization Key workflows. It still contains
`UserClient`, User API Key management and Schedulers; treat them as subject to
removal before `0.3.0`.

- Rename the package to `@tidblink/server`, published under the `@tidblink` npm
  organization. It is the package for backends that hold an Organization API
  Key. The earlier `@mem9/tidb-link` name was never published.
- Release by merging a release pull request: a package version with its own
  changelog section and no release tag is staged on npm (`next` for prereleases,
  `latest` otherwise) and becomes public after a maintainer approves it there.
  The workflow then records a `@tidblink/server@<version>` tag and a GitHub
  Release. Pushing a tag no longer publishes.
- Add `OrganizationClient.user(userId)` for the existing supported End User
  resource workflows without issuing a User Key. Retain `UserClient` compatibility,
  real authorizing credentials, owner-scoped handles and separate usage projections.
- Pin the supported contract to service
  `882287809f91d9a3fdf60537460431d9f0fc23e7` and OpenAPI SHA-256
  `906515e4c8d38c5ac9fce6390d9d646220b2d06100fbcb64e180419899989a5b`.
- Include `organization_user` billing attribution and deleted Scheduler history.
  Add a packaged Organization example and a compiled-backend independent packed
  consumer for the supported journey. MCP, general Drive management and WebSocket
  media transport remain outside the SDK. No package publication is performed.

- Bind User API Key creation, rotation and revocation receipts to their requested
  targets. Validate credential status and token/key identity, preserving unknown
  outcomes and request IDs without automatically replaying credential mutations.

- Consolidate service compatibility metadata under `TIDB_LINK_REVISION`,
  `tidbLinkRevision` and `tidbLinkOpenApiSha256`. Remove the Provisioning,
  Realtime, Lifecycle and Text revision exports and duplicate package fields;
  the selected service revision and contract fingerprints are unchanged.

- Refresh the Node.js ESM SDK for exact Agent Service revision
  `0d11f1b231879c735bd53b60ff895ac58ba60d33`, retaining Node.js 22.12 or newer
  and no runtime dependencies. The package remains unpublished by this change.
- Separate Organization and User authority. Support Organization information,
  End User provisioning and User API Key creation, reads, rotation and revocation,
  preserving uncertain outcomes and one-time credential delivery.
- Discover Templates/models; manage independent Agents and owned Sessions with
  stable creation identities, model revision checks, names and Billing Tags.
  Organization callers can explicitly unlink a target User's Agent.
- Submit text and steer active work; wait for or observe original results with
  resumable event positions and replacement drafts. Support explicit cancellation,
  checkpoint retry and paginated interaction/Turn/Timeline history without
  treating acceptance, EOF or an abandoned wait as success or cancellation.
- Declare/replace Client Tools, recover pending calls and explicitly submit the
  application's successful or failed outputs; no automatic tool execution.
- Upload inline, multipart and empty files with complete integrity checks,
  cancellation and original-key recovery; attach ready files and inspect or
  download their content without forwarding API Keys to grant-only destinations.
- Manage Schedulers with creation identity, conditional updates, pagination and
  fire history. Read User and Organization target-User usage without changing
  unknown, unpriced, incomplete or estimated facts.
- Submit prerecorded audio with its own interruption semantics, and prepare/read/
  end Realtime Connections with one-time credentials. Audio transport, capture,
  playback, browser, MSP and management integrations remain excluded.
- Reject hidden JavaScript transport/retry options and retain original identities
  across caller mutation. Verify shipped examples and the composed application
  quickstart using the installed packed SDK against the exact real service;
  independent TypeScript consumers and frozen contracts supplement that evidence.
- Remove obsolete Workspace/ServiceUser, Template-version/Memory/default-Agent
  and compatibility exports. Use the current `@tidblink/server` package and typed
  `ti_org_<id>_<secret>` / `ti_user_<id>_<secret>` credential grammar.

## 0.2.0

- Replace Workspace and Project authority with Organization and User authority.
- Match the Pi-only Agent, Agent-bound Session, Session Model, Clarification,
  result, and Memory contract in Agent Service revision
  `9c1e9aceb28afce1166e4249ea104c7a2f8aac73`.
- Remove SDK fields for Runtime selection, Codex, Agent Instructions, structured
  Turn output, and other unsupported capabilities.
- Verify pull requests, releases, and packed artifacts against the immutable
  compatible Agent Service OpenAPI contract.
