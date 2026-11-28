"""Realtime Model selection and connection grants (``realtime.ts`` in the TypeScript SDK)."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Literal, NoReturn, NotRequired, TypedDict
from urllib.parse import urlsplit

from .errors import OutcomeUnknownError

RealtimeLanguage = Literal["zh-CN", "en-US"]
RealtimeEndpointingMode = Literal["client", "server"]


class RealtimePcm16AudioFormat(TypedDict):
    encoding: Literal["pcm16"]
    sampleRate: int
    channels: Literal[1]


class RealtimeOpusAudioFormat(TypedDict):
    encoding: Literal["opus"]
    sampleRate: int
    channels: Literal[1]
    packetDurationMs: int
    bitrate: NotRequired[Literal[16000, 24000, 32000]]


RealtimeAudioFormat = RealtimePcm16AudioFormat | RealtimeOpusAudioFormat


class RealtimeAudioFormats(TypedDict):
    input: RealtimeAudioFormat
    output: RealtimeAudioFormat


class RealtimeOpenAudio(TypedDict, total=False):
    """The audio formats to open a Realtime Connection with; either may be omitted."""

    input: RealtimeAudioFormat
    output: RealtimeAudioFormat


class RealtimeModel(TypedDict):
    id: str
    label: str
    available: bool
    isDefault: bool
    languages: list[RealtimeLanguage]
    endpointingModes: list[RealtimeEndpointingMode]
    audio: dict[str, list[RealtimeAudioFormat]]
    #: Ids of the Realtime Voices this model speaks.
    voices: list[str]


class RealtimeVoice(TypedDict):
    """A published Realtime Voice; supplier voices are never exposed."""

    id: str
    label: str
    description: str | None


class RealtimeModelCatalog(TypedDict):
    defaultModel: str | None
    #: The release default voice, which every published model speaks.
    defaultVoice: str | None
    voices: list[RealtimeVoice]
    models: list[RealtimeModel]


class RealtimeModelSelection(TypedDict):
    model: str | None
    #: The selected Realtime Voice id; None speaks the release default voice.
    voice: str | None
    revision: int
    etag: str
    #: The Session's fixed Realtime Language; None when its recipe disables Realtime.
    language: RealtimeLanguage | None


class RealtimeConnection(TypedDict):
    object: Literal["realtime_connection"]
    id: str
    sessionId: str
    provider: dict[str, str | None]
    status: Literal["connecting", "active", "paused", "ended"]
    audio: RealtimeAudioFormats
    activeTaskTurnId: str | None
    usage: dict[str, float]
    metadata: dict[str, str]
    createdAt: str
    endedAt: str | None


class RealtimeConnectGrant(TypedDict):
    websocketUrl: str
    connectToken: str
    connectTokenExpiresAt: str


class OpenRealtimeConnectionResult(RealtimeConnectGrant):
    realtimeConnection: RealtimeConnection


def realtime_model_selection_from(session: dict[str, Any]) -> RealtimeModelSelection:
    return {
        "model": session.get("realtimeModel"),
        "voice": session.get("realtimeVoice"),
        "revision": session.get("realtimeModelRevision"),  # type: ignore[typeddict-item]
        "etag": session.get("realtimeModelEtag"),  # type: ignore[typeddict-item]
        "language": session.get("realtimeLanguage"),
    }


def _is_string(value: object) -> bool:
    return isinstance(value, str) and len(value) > 0


def _is_nullable_string(value: object) -> bool:
    return value is None or isinstance(value, str)


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _is_integer(value: object) -> bool:
    return _is_number(value) and float(value).is_integer()  # type: ignore[arg-type]


def is_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _is_audio_format(value: object) -> bool:
    if (
        not isinstance(value, dict)
        or not _is_number(value.get("sampleRate"))
        or value["sampleRate"] <= 0
        or value.get("channels") != 1
        or isinstance(value.get("channels"), bool)
    ):
        return False
    if value.get("encoding") == "pcm16":
        return True
    return (
        value.get("encoding") == "opus"
        and _is_integer(value.get("packetDurationMs"))
        and value["packetDurationMs"] > 0
        and ("bitrate" not in value or value["bitrate"] in (16000, 24000, 32000))
    )


def _is_realtime_connection(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    provider = value.get("provider")
    audio = value.get("audio")
    usage = value.get("usage")
    metadata = value.get("metadata")
    return (
        value.get("object") == "realtime_connection"
        and _is_string(value.get("id"))
        and _is_string(value.get("sessionId"))
        and isinstance(provider, dict)
        and _is_string(provider.get("name"))
        and "region" in provider
        and _is_nullable_string(provider["region"])
        and "model" in provider
        and _is_nullable_string(provider["model"])
        and value.get("status") in ("connecting", "active", "paused", "ended")
        and isinstance(audio, dict)
        and _is_audio_format(audio.get("input"))
        and _is_audio_format(audio.get("output"))
        and "activeTaskTurnId" in value
        and _is_nullable_string(value["activeTaskTurnId"])
        and isinstance(usage, dict)
        and _is_number(usage.get("inputAudioSec"))
        and _is_number(usage.get("outputAudioSec"))
        and _is_integer(usage.get("thoughtTokens"))
        and _is_integer(usage.get("textTokens"))
        and isinstance(metadata, dict)
        and all(isinstance(entry, str) for entry in metadata.values())
        and is_date_time(value.get("createdAt"))
        and "endedAt" in value
        and (value["endedAt"] is None or is_date_time(value["endedAt"]))
    )


_CONNECT_TOKEN = re.compile(r"rct_[A-Za-z0-9_-]{43}")


def is_connect_grant(value: object) -> bool:
    if not isinstance(value, dict) or not _is_string(value.get("websocketUrl")):
        return False
    try:
        scheme = urlsplit(value["websocketUrl"]).scheme
    except ValueError:
        return False
    token = value.get("connectToken")
    return (
        scheme in ("ws", "wss")
        and isinstance(token, str)
        and bool(_CONNECT_TOKEN.fullmatch(token))
        and is_date_time(value.get("connectTokenExpiresAt"))
    )


def _unusable_receipt(operation: str, request_id: str | None) -> NoReturn:
    raise OutcomeUnknownError(
        f"TiDB Link accepted {operation} but returned an unusable receipt", request_id=request_id
    )


def require_realtime_model_selection_receipt(value: object, request_id: str | None) -> RealtimeModelSelection:
    if not isinstance(value, dict):
        _unusable_receipt("the Realtime Model selection", request_id)
    language = value.get("realtimeLanguage", 0)
    revision = value.get("realtimeModelRevision")
    if (
        "realtimeModel" not in value
        or not _is_nullable_string(value["realtimeModel"])
        or "realtimeVoice" not in value
        or not _is_nullable_string(value["realtimeVoice"])
        or not _is_integer(revision)
        or revision <= 0  # type: ignore[operator]
        or not _is_string(value.get("realtimeModelEtag"))
        or (language is not None and language not in ("zh-CN", "en-US"))
    ):
        _unusable_receipt("the Realtime Model selection", request_id)
    return realtime_model_selection_from(value)


def require_open_realtime_connection_receipt(value: object, request_id: str | None) -> OpenRealtimeConnectionResult:
    if (
        not isinstance(value, dict)
        or not _is_realtime_connection(value.get("realtimeConnection"))
        or not is_connect_grant(value)
    ):
        _unusable_receipt("the Realtime Connection request", request_id)
    return value  # type: ignore[return-value]


def require_realtime_connect_grant_receipt(value: object, request_id: str | None) -> RealtimeConnectGrant:
    if not is_connect_grant(value):
        _unusable_receipt("the Realtime connect-token request", request_id)
    return value  # type: ignore[return-value]


def require_realtime_connection_receipt(value: object, request_id: str | None) -> RealtimeConnection:
    if not isinstance(value, dict) or not _is_realtime_connection(value.get("realtimeConnection")):
        _unusable_receipt("the Realtime Connection mutation", request_id)
    return value["realtimeConnection"]
