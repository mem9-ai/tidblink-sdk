"""A Session handle and Session creation (``session.ts`` in the TypeScript SDK)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from ._http import (
    HttpClient,
    require_creation_key,
    require_etag,
    require_known_keys,
    require_template_id,
    segment,
)
from ._types import OMIT, Omittable
from .client_tools import (
    ClientToolDeclaration,
    ClientToolSet,
    get_client_tools,
    replace_client_tools,
    validate_client_tools,
)
from .control import RetryReceipt, submit_turn_command
from .errors import OutcomeUnknownError
from .history import (
    BillingTag,
    SessionInteraction,
    SessionInteractionPage,
    SessionTimelinePage,
    TurnPage,
    TurnRecord,
    TurnState,
    get_interaction,
    get_turn,
    get_turn_state,
    history,
    list_interactions,
    reconcile_interaction,
    timeline,
)
from .realtime import (
    OpenRealtimeConnectionResult,
    RealtimeConnectGrant,
    RealtimeConnection,
    RealtimeModelSelection,
    RealtimeOpenAudio,
    RealtimeAudioFormats,
    RealtimeVoiceSetting,
    realtime_model_selection_from,
    require_open_realtime_connection_receipt,
    require_realtime_connect_grant_receipt,
    require_realtime_connection_receipt,
    require_realtime_model_selection_receipt,
)
from .template_switch import TemplateSwitchReceipt, switch_template
from .turn import TurnResult, message_data, submit_message, validate_wait_options, wait_for_result

if TYPE_CHECKING:
    from ._http import Bytes
    from .audio import AudioTurnEvent, AudioTurnResult
    from .events import RealtimeResponse, RealtimeTranscript, SessionEvent
    from .session_connection import ClientToolHandler, SessionConnection


#: Model IDs come from the deployment catalog, including newly published models.
SelectableSessionModel = str
SessionModel = str


class SessionSandboxSelection(TypedDict, total=False):
    """The Sandbox a Session runs in, chosen at creation."""

    #: A selectable Sandbox profile; omitted or None uses the Template's profile.
    profileId: str | None
    #: A Drive-relative working directory; defaults to ``.``. Creation does not make it.
    workingDirectory: str


class SessionTemplate(TypedDict):
    templateId: str
    contentDigest: str


class SessionRealtime(TypedDict):
    id: str
    status: Literal["connecting", "active", "paused"]
    audio: RealtimeAudioFormats
    startedAt: str


class SessionRecord(TypedDict):
    organizationId: str
    sessionId: str
    name: str | None
    autoTitle: str | None
    ownerUserId: str
    startedByUserId: str
    createdByType: Literal["user", "scheduler", "template_preview"]
    readOnly: bool
    sourceSchedulerId: str | None
    sourceSchedulerFireId: str | None
    billingTag: BillingTag | None
    model: str
    modelRevision: int
    realtimeModel: str | None
    realtimeModelRevision: int
    realtimeModelEtag: str
    realtimeVoice: RealtimeVoiceSetting | None
    template: NotRequired[SessionTemplate | None]
    realtime: SessionRealtime | None
    #: Current Session Configuration Epoch; None for read-only or provider-owned snapshots.
    configurationEpoch: int | None
    #: True while a Template switch fences new interaction.
    switching: bool
    modelPolicyStatus: Literal["allowed", "organization_disallowed"]
    status: Literal["active", "deleted"]
    activeTurnId: str | None
    createdAt: str
    updatedAt: str
    deletedAt: str | None


def _session_receipt(value: Any, request_id: str) -> SessionRecord:
    session = value.get("session") if isinstance(value, dict) else None
    if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str) or not session["sessionId"]:
        raise OutcomeUnknownError(
            "Session mutation returned no usable Session; reconcile before retrying",
            request_id=request_id,
        )
    return session  # type: ignore[return-value]


class Session:
    """A handle to one Session of the selected End User.

    ``data`` holds the last Session record this handle read or changed, if any.
    """

    def __init__(
        self,
        http: HttpClient,
        id: str,
        record: SessionRecord | None = None,
        etag: str | None = None,
    ) -> None:
        if not isinstance(id, str):
            raise TypeError("Session id is required")
        if not id:
            raise ValueError("Session id is required")
        self._http = http
        self.id = id
        self._record = record
        self._etag = etag

    def __repr__(self) -> str:
        return f"Session(id={self.id!r})"

    @property
    def data(self) -> SessionRecord | None:
        return self._record

    @property
    def model(self) -> str | None:
        return self._record["model"] if self._record else None

    @property
    def _path(self) -> str:
        return f"/sessions/{segment(self.id)}"

    async def refresh(self, *, request_id: str | None = None) -> Session:
        result = await self._http.get_response(self._path, request_id=request_id)
        etag = require_etag(result)
        self._record = result.data["session"]
        self._etag = etag
        return self

    async def set_model(self, model: str, *, request_id: str | None = None) -> Session:
        """Select the model of the next Turn with the model ETag this handle retains, reading it first if needed.

        Another handle's intervening change raises :class:`ConflictError`; it is never retried.
        """
        if not self._etag:
            await self.refresh(request_id=request_id)
        result = await self._http.patch_response(
            f"{self._path}/model",
            {"model": model},
            if_match=self._etag,
            outcome_unknown=True,
            request_id=request_id,
        )
        if not result.etag:
            raise OutcomeUnknownError(
                "Session model update returned no model ETag; read the Session before changing it again",
                request_id=result.request_id,
            )
        self._record = _session_receipt(result.data, result.request_id)
        self._etag = result.etag
        return self

    async def rename(self, name: str, *, request_id: str | None = None) -> Session:
        result = await self._http.post_response(
            f"{self._path}/name", {"name": name}, outcome_unknown=True, request_id=request_id
        )
        self._record = _session_receipt(result.data, result.request_id)
        return self

    async def suggest_name(
        self,
        *,
        user_text: str,
        assistant_text: Omittable[str] = OMIT,
        request_id: str | None = None,
    ) -> Session:
        """Generate and save a name from the conversation's first exchange."""
        body: dict[str, str] = {"userText": user_text}
        if assistant_text is not OMIT:
            body["assistantText"] = assistant_text
        result = await self._http.post_response(
            f"{self._path}/name/suggest", body, outcome_unknown=True, request_id=request_id
        )
        self._record = _session_receipt(result.data, result.request_id)
        return self

    async def set_billing_tag(self, billing_tag: BillingTag | None, *, request_id: str | None = None) -> Session:
        result = await self._http.patch_response(
            f"{self._path}/billing-tag",
            {"billingTag": billing_tag},
            outcome_unknown=True,
            request_id=request_id,
        )
        self._record = _session_receipt(result.data, result.request_id)
        return self

    async def delete(self, *, request_id: str | None = None) -> Session:
        """Delete the Session; ``data`` then holds the deleted tombstone."""
        result = await self._http.delete_response(self._path, outcome_unknown=True, request_id=request_id)
        self._record = _session_receipt(result.data, result.request_id)
        return self

    # Realtime -----------------------------------------------------------------

    async def open_realtime_connection(
        self,
        *,
        audio: Omittable[RealtimeOpenAudio] = OMIT,
        metadata: Omittable[Mapping[str, str]] = OMIT,
    ) -> OpenRealtimeConnectionResult:
        body: dict[str, Any] = {}
        if audio is not OMIT:
            body["audio"] = require_known_keys(audio, ("input", "output"), "audio")
        if metadata is not OMIT:
            body["metadata"] = dict(metadata)
        result = await self._http.post_response(f"{self._path}/realtime", body, outcome_unknown=True)
        return require_open_realtime_connection_receipt(result.data, result.request_id)

    async def get_realtime_connection(self, *, request_id: str | None = None) -> RealtimeConnection:
        """The Session's newest Realtime Connection."""
        response = await self._http.get(f"{self._path}/realtime", request_id=request_id)
        return response["realtimeConnection"]

    async def create_realtime_connect_token(self) -> RealtimeConnectGrant:
        result = await self._http.post_response(f"{self._path}/realtime/connect-tokens", outcome_unknown=True)
        return require_realtime_connect_grant_receipt(result.data, result.request_id)

    async def end_realtime_connection(self) -> RealtimeConnection:
        result = await self._http.delete_response(f"{self._path}/realtime", outcome_unknown=True)
        return require_realtime_connection_receipt(result.data, result.request_id)

    async def get_realtime_model_selection(self, *, request_id: str | None = None) -> RealtimeModelSelection:
        response = await self._http.get(self._path, request_id=request_id)
        return realtime_model_selection_from(response["session"])

    async def set_realtime_model(self, model: str, *, if_match: str) -> RealtimeModelSelection:
        if not isinstance(model, str) or not model or len(model) > 128:
            raise ValueError("Realtime Model ID must contain 1-128 characters")
        if not isinstance(if_match, str) or not if_match:
            raise ValueError("Realtime Model selection requires if_match")
        result = await self._http.patch_response(
            f"{self._path}/realtime-model",
            {"realtimeModel": model},
            if_match=if_match,
            outcome_unknown=True,
        )
        receipt = result.data.get("session") if isinstance(result.data, dict) else None
        return require_realtime_model_selection_receipt(receipt, result.request_id)

    # Template switch ------------------------------------------------------------

    async def switch_template(
        self,
        *,
        template_id: str,
        idempotency_key: str,
        acknowledge_session_recall: Omittable[bool] = OMIT,
        request_id: str | None = None,
    ) -> TemplateSwitchReceipt:
        """Move this idle Session to another Template.

        After a lost response, repeat with the same input to recover the
        receipt. The receipt holds no Session, so ``data`` is not updated: call
        :meth:`refresh` to read the new Template.
        """
        return await switch_template(
            self._http,
            self.id,
            template_id=template_id,
            idempotency_key=idempotency_key,
            acknowledge_session_recall=acknowledge_session_recall,
            request_id=request_id,
        )

    # Backend Connection ---------------------------------------------------------

    async def connect(
        self,
        *,
        tools: Mapping[str, ClientToolHandler] | None = None,
        observe: bool = False,
        cursor: str | None = None,
        on_cursor: Callable[[str], Awaitable[None] | None] | None = None,
        on_event: Callable[[SessionEvent], Awaitable[None] | None] | None = None,
        on_transcript: Callable[[RealtimeTranscript], Awaitable[None] | None] | None = None,
        on_response: Callable[[RealtimeResponse], Awaitable[None] | None] | None = None,
        max_buffered_bytes: int | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> SessionConnection:
        """Attach this Session to the process's Backend Connection.

        Receive its events and, unless ``observe`` is set, declare and execute
        its Client Tools. Returns once the attach took effect. One connection
        per Session per ``OrganizationClient``; a connection on another process
        or client takes the Session over and this one ends with ``superseded``.
        """
        from .session_connection import connect_session

        return await connect_session(
            self._http,
            self.id,
            tools=tools,
            observe=observe,
            cursor=cursor,
            on_cursor=on_cursor,
            on_event=on_event,
            on_transcript=on_transcript,
            on_response=on_response,
            max_buffered_bytes=max_buffered_bytes,
            on_error=on_error,
        )

    # Prerecorded audio ----------------------------------------------------------

    def stream_audio(
        self,
        data: Bytes,
        *,
        filename: str,
        content_type: str | None = None,
        billing_tag: Omittable[BillingTag | None] = OMIT,
        request_id: str | None = None,
    ) -> AsyncIterator[AudioTurnEvent]:
        """The audio Turn's events, as the request's own response delivers them.

        Leaving the iteration early may request interruption of the Turn.
        """
        from .audio import stream_audio

        return stream_audio(
            self._http,
            self.id,
            data,
            filename=filename,
            content_type=content_type,
            billing_tag=billing_tag,
            request_id=request_id,
        )

    async def audio(
        self,
        data: Bytes,
        *,
        filename: str,
        content_type: str | None = None,
        billing_tag: Omittable[BillingTag | None] = OMIT,
        request_id: str | None = None,
    ) -> AudioTurnResult:
        from .audio import audio_turn

        return await audio_turn(
            self._http,
            self.id,
            data,
            filename=filename,
            content_type=content_type,
            billing_tag=billing_tag,
            request_id=request_id,
        )

    # Client Tools ---------------------------------------------------------------

    async def get_client_tools(self, *, request_id: str | None = None) -> ClientToolSet:
        return await get_client_tools(self._http, self.id, request_id=request_id)

    async def replace_client_tools(
        self,
        tools: Sequence[ClientToolDeclaration],
        *,
        if_match: str,
        request_id: str | None = None,
    ) -> ClientToolSet:
        return await replace_client_tools(self._http, self.id, tools, if_match=if_match, request_id=request_id)

    # History --------------------------------------------------------------------

    async def history(
        self, *, limit: int | None = None, cursor: str | None = None, request_id: str | None = None
    ) -> TurnPage:
        return await history(self._http, self.id, limit=limit, cursor=cursor, request_id=request_id)

    async def get_turn(self, turn_id: str, *, request_id: str | None = None) -> TurnRecord:
        return await get_turn(self._http, self.id, turn_id, request_id=request_id)

    async def get_turn_state(self, turn_id: str, *, request_id: str | None = None) -> TurnState:
        """Read a Turn with ``stop_requested_at``, when a request to stop its current execution was accepted.

        A Turn can still be running after a stop request.
        """
        return await get_turn_state(self._http, self.id, turn_id, request_id=request_id)

    async def timeline(
        self, *, limit: int | None = None, cursor: str | None = None, request_id: str | None = None
    ) -> SessionTimelinePage:
        return await timeline(self._http, self.id, limit=limit, cursor=cursor, request_id=request_id)

    async def list_interactions(
        self, *, limit: int | None = None, cursor: str | None = None, request_id: str | None = None
    ) -> SessionInteractionPage:
        return await list_interactions(self._http, self.id, limit=limit, cursor=cursor, request_id=request_id)

    async def get_interaction(self, input_id: str, *, request_id: str | None = None) -> SessionInteraction:
        return await get_interaction(self._http, self.id, input_id, request_id=request_id)

    async def reconcile_interaction(
        self, client_event_id: str, *, request_id: str | None = None
    ) -> SessionInteraction | None:
        """The receipt of ``client_event_id``, or None when none was visible at this read."""
        return await reconcile_interaction(self._http, self.id, client_event_id, request_id=request_id)

    # Commands -------------------------------------------------------------------

    async def cancel(
        self, *, client_event_id: str, turn_id: str, request_id: str | None = None
    ) -> SessionInteraction:
        """Request cancellation of an exact Turn; read the receipt for the eventual outcome."""
        return await submit_turn_command(  # type: ignore[return-value]
            self._http, self.id, "cancel", client_event_id=client_event_id, turn_id=turn_id, request_id=request_id
        )

    async def retry(self, *, client_event_id: str, turn_id: str, request_id: str | None = None) -> RetryReceipt:
        """Start another execution of the Turn from its checkpoint, with its own identity and result."""
        return await submit_turn_command(  # type: ignore[return-value]
            self._http, self.id, "retry", client_event_id=client_event_id, turn_id=turn_id, request_id=request_id
        )

    async def submit_message(
        self,
        *,
        client_event_id: str,
        text: str | Sequence[str],
        user_file_ids: Omittable[Sequence[str]] = OMIT,
        clarification_source_turn_id: Omittable[str] = OMIT,
        billing_tag: Omittable[BillingTag | None] = OMIT,
        request_id: str | None = None,
    ) -> SessionInteraction:
        """Submit a text message and return its receipt. Acceptance does not mean incorporated or completed."""
        data = message_data(
            text,
            user_file_ids=user_file_ids,
            clarification_source_turn_id=clarification_source_turn_id,
            billing_tag=billing_tag,
        )
        return await submit_message(
            self._http, self.id, client_event_id=client_event_id, data=data, request_id=request_id
        )

    async def wait_for_result(
        self,
        input_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
        request_id: str | None = None,
    ) -> TurnResult:
        """Wait for the original result of a message or checkpoint retry.

        ``timeout`` (default 60 seconds) is a local deadline that never cancels
        execution; ``poll_interval`` defaults to 0.25 seconds.
        """
        return await wait_for_result(
            self._http,
            self.id,
            input_id,
            timeout=timeout,
            poll_interval=poll_interval,
            request_id=request_id,
        )

    async def turn(
        self,
        *,
        client_event_id: str,
        text: str | Sequence[str],
        user_file_ids: Omittable[Sequence[str]] = OMIT,
        clarification_source_turn_id: Omittable[str] = OMIT,
        billing_tag: Omittable[BillingTag | None] = OMIT,
        timeout: float | None = None,
        poll_interval: float | None = None,
        request_id: str | None = None,
    ) -> TurnResult:
        """Submit a message and wait for its result.

        ``request_id`` identifies the submission; the reads that wait for the
        result send their own, as ``wait_for_result`` does without one.
        """
        validate_wait_options(timeout, poll_interval)
        accepted = await self.submit_message(
            client_event_id=client_event_id,
            text=text,
            user_file_ids=user_file_ids,
            clarification_source_turn_id=clarification_source_turn_id,
            billing_tag=billing_tag,
            request_id=request_id,
        )
        return await self.wait_for_result(accepted["inputId"], timeout=timeout, poll_interval=poll_interval)


async def create_session(
    http: HttpClient,
    *,
    template_id: str,
    idempotency_key: str,
    model: Omittable[str] = OMIT,
    realtime_model: Omittable[str] = OMIT,
    billing_tag: Omittable[BillingTag | None] = OMIT,
    tools: Omittable[Sequence[ClientToolDeclaration]] = OMIT,
    additional_instructions: Omittable[str | None] = OMIT,
    sandbox: Omittable[SessionSandboxSelection] = OMIT,
    acknowledge_session_recall: Omittable[bool] = OMIT,
    request_id: str | None = None,
) -> Session:
    require_template_id(template_id, "Session creation")
    require_creation_key(idempotency_key, "Session creation")
    body: dict[str, Any] = {"agentTemplateId": template_id}
    if model is not OMIT:
        body["model"] = model
    if realtime_model is not OMIT:
        body["realtimeModel"] = realtime_model
    if billing_tag is not OMIT:
        body["billingTag"] = billing_tag
    if tools is not OMIT:
        body["tools"] = validate_client_tools(tools)
    if additional_instructions is not OMIT:
        if additional_instructions is not None and not isinstance(additional_instructions, str):
            raise TypeError("additional_instructions must be text or None")
        body["additionalInstructions"] = additional_instructions
    if sandbox is not OMIT:
        selection = require_known_keys(sandbox, ("profileId", "workingDirectory"), "Session Sandbox selection")
        profile_id = selection.get("profileId")
        if profile_id is not None and not isinstance(profile_id, str):
            raise TypeError("sandbox profileId must be text or None")
        if "workingDirectory" in selection and not isinstance(selection["workingDirectory"], str):
            raise TypeError("sandbox workingDirectory must be text")
        body["sandbox"] = selection
    if acknowledge_session_recall is not OMIT:
        body["acknowledgeSessionRecall"] = acknowledge_session_recall
    result = await http.post_response(
        "/sessions",
        body,
        idempotency_key=idempotency_key,
        idempotent=True,
        outcome_unknown=True,
        request_id=request_id,
    )
    record = _session_receipt(result.data, result.request_id)
    if not result.etag:
        raise OutcomeUnknownError(
            "Session creation returned no model ETag; recover with the same creation key",
            request_id=result.request_id,
        )
    return Session(http, record["sessionId"], record, result.etag)
