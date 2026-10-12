"""Authenticated realtime STT WebSocket endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.logging import get_logger
from app.core.security import TokenService
from app.db.session import Database
from app.models.enums import MeetingPermission, MeetingSourceType, MeetingStatus
from app.models.user import User
from app.providers.contracts import (
    StreamingOptions,
    StreamingTranscriptionEvent,
    STTProvider,
    STTProviderError,
    STTStream,
    TranscriptionSegment,
    TranslationProvider,
)
from app.providers.storage import MediaStorage
from app.services.meetings import MeetingService
from app.services.realtime import RealtimeTranscriptService
from app.services.realtime_recording import (
    RealtimeRecordingError,
    RealtimeRecordingService,
)
from app.services.realtime_translation import (
    RealtimeTranslationItem,
    RealtimeTranslationSession,
)

router = APIRouter()
SendJson = Callable[[dict[str, object]], Awaitable[None]]
logger = get_logger(__name__)


@dataclass(frozen=True)
class RealtimeCommand:
    type: str
    target_language: str | None = None
    logical_segment_ids: tuple[UUID, ...] = ()


@router.websocket("/realtime/{meeting_id}")
async def realtime_transcription(websocket: WebSocket, meeting_id: UUID) -> None:
    """Accept binary audio and emit normalized partial/final transcript events."""
    await websocket.accept()
    stream: STTStream | None = None
    transcript_service: RealtimeTranscriptService | None = None
    recording_session: RealtimeRecordingService | None = None
    translation_session: RealtimeTranslationSession | None = None
    terminal_failure = False
    retryable_interruption = False
    send_lock = asyncio.Lock()

    async def send_json(payload: dict[str, object]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    try:
        settings = cast(Settings, websocket.app.state.settings)
        provider = cast(STTProvider | None, getattr(websocket.app.state, "stt_provider", None))
        translation_provider = cast(
            TranslationProvider | None,
            getattr(websocket.app.state, "translation_provider", None),
        )
        if provider is None:
            await _error_and_close(
                websocket,
                send_json,
                "PROVIDER_CAPABILITY_UNAVAILABLE",
                "Realtime transcription is not configured.",
                retryable=False,
            )
            return
        if not provider.capabilities().streaming:
            await _error_and_close(
                websocket,
                send_json,
                "PROVIDER_CAPABILITY_UNAVAILABLE",
                "The configured STT provider does not support streaming.",
                retryable=False,
            )
            return
        user = await _websocket_user(websocket, settings)
        database = cast(Database, websocket.app.state.database)
        async with database.session() as session:
            meeting, _ = await MeetingService(session).require_access(
                meeting_id, user, MeetingPermission.EDIT
            )
            if meeting.source_type != MeetingSourceType.REALTIME:
                await _error_and_close(
                    websocket,
                    send_json,
                    "MEETING_SOURCE_TYPE_MISMATCH",
                    "Realtime transcription is only available for realtime meetings.",
                    retryable=False,
                )
                return
            if meeting.status == MeetingStatus.COMPLETED:
                await _error_and_close(
                    websocket,
                    send_json,
                    "MEETING_ALREADY_COMPLETED",
                    "This realtime meeting has already been completed.",
                    retryable=False,
                )
                return
            options = StreamingOptions(
                language=meeting.language_mode,
                diarization=meeting.diarization_enabled,
                context=". ".join(
                    item.strip()
                    for item in (meeting.title, meeting.description or "")
                    if item.strip()
                ),
                keyterms=(meeting.title,),
            )
            try:
                stream = await provider.open_stream(options)
            except STTProviderError as exc:
                if not exc.retryable:
                    await RealtimeTranscriptService(session, meeting, user.id).recording_failed()
                await _handle_provider_error(websocket, send_json, exc)
                return
            except Exception:
                await _handle_provider_error(
                    websocket,
                    send_json,
                    STTProviderError("The STT provider could not start a realtime session."),
                )
                return
            recording_session = RealtimeRecordingService(
                session,
                meeting,
                cast(MediaStorage, websocket.app.state.media_storage),
            )
            await recording_session.start()
            transcript_service = RealtimeTranscriptService(session, meeting, user.id)
            if translation_provider is not None:
                translation_session = RealtimeTranslationSession(
                    translation_provider,
                    meeting.language_mode,
                    send_json,
                )
            await transcript_service.recording_started()
            await send_json(
                {
                    "type": "session.ready",
                    "provider": settings.stt_provider or provider.__class__.__name__,
                    "language": meeting.language_mode,
                }
            )
            paused = False
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    if paused:
                        await _send_error(
                            send_json,
                            "SESSION_PAUSED",
                            "Resume recording before sending audio.",
                            False,
                        )
                        continue
                    try:
                        audio_chunk = message["bytes"]
                        recording_session.write(audio_chunk)
                        events = await stream.send_audio(audio_chunk)
                        await _emit_events(
                            send_json,
                            transcript_service,
                            events,
                            translation_session=translation_session,
                            timeline_offset_ms=recording_session.timeline_offset_ms,
                        )
                    except STTProviderError as exc:
                        terminal_failure = not exc.retryable
                        retryable_interruption = exc.retryable
                        if terminal_failure:
                            await transcript_service.recording_failed()
                        await _flush_interrupted_stream(
                            stream,
                            transcript_service,
                            recording_session.timeline_offset_ms,
                        )
                        stream = None
                        await _save_recording(recording_session, meeting.id)
                        await _handle_provider_error(websocket, send_json, exc)
                        return
                    except RealtimeRecordingError:
                        retryable_interruption = True
                        await _error_and_close(
                            websocket,
                            send_json,
                            "AUDIO_STORAGE_ERROR",
                            "The realtime recording could not be saved.",
                            True,
                        )
                        return
                    except ValueError as exc:
                        await _send_error(send_json, "STT_RESULT_INVALID", str(exc), False)
                    except Exception:
                        logger.exception(
                            "realtime_audio_chunk_failed",
                            extra={"meeting_id": str(meeting.id)},
                        )
                        retryable_interruption = True
                        await _flush_interrupted_stream(
                            stream,
                            transcript_service,
                            recording_session.timeline_offset_ms,
                        )
                        stream = None
                        await _save_recording(recording_session, meeting.id)
                        await _handle_provider_error(
                            websocket,
                            send_json,
                            STTProviderError(
                                "The STT provider could not process this audio chunk."
                            ),
                        )
                        return
                    continue
                if message.get("text") is None:
                    await _send_error(
                        send_json, "INVALID_MESSAGE", "Audio chunks must be binary.", False
                    )
                    continue
                command = _command(message["text"])
                if command is None:
                    await _send_error(
                        send_json, "INVALID_MESSAGE", "Realtime command is invalid.", False
                    )
                    continue
                if command.type == "translation.enable":
                    target = command.target_language
                    if (
                        translation_provider is None
                        or translation_session is None
                        or target is None
                    ):
                        await _send_translation_error(
                            send_json,
                            "PROVIDER_CAPABILITY_UNAVAILABLE",
                            "Realtime translation is not configured.",
                            False,
                        )
                        continue
                    if not _translation_configuration_supported(
                        meeting.language_mode,
                        target,
                        translation_provider.capabilities().supported_language_pairs,
                    ):
                        await _send_translation_error(
                            send_json,
                            "PROVIDER_CAPABILITY_UNAVAILABLE",
                            "This realtime translation language pair is not supported.",
                            False,
                        )
                        continue
                    try:
                        trusted = await transcript_service.translation_inputs(
                            command.logical_segment_ids
                        )
                    except ValueError as exc:
                        await _send_translation_error(
                            send_json,
                            "TRANSLATION_SEGMENT_NOT_FOUND",
                            str(exc),
                            False,
                            command.logical_segment_ids,
                        )
                        continue
                    await translation_session.enable(
                        target,
                        (
                            RealtimeTranslationItem(logical_id, segment)
                            for logical_id, segment in trusted
                        ),
                    )
                    await send_json(
                        {
                            "type": "translation.status",
                            "enabled": True,
                            "source_language": meeting.language_mode,
                            "target_language": target,
                        }
                    )
                    continue
                if command.type == "translation.disable":
                    if translation_session is not None:
                        await translation_session.disable()
                    await send_json({"type": "translation.status", "enabled": False})
                    continue
                try:
                    if command.type == "recording.pause":
                        paused = True
                        await _emit_events(
                            send_json,
                            transcript_service,
                            await stream.pause(),
                            translation_session=translation_session,
                            timeline_offset_ms=recording_session.timeline_offset_ms,
                        )
                    elif command.type == "recording.resume":
                        paused = False
                        await _emit_events(
                            send_json,
                            transcript_service,
                            await stream.resume(),
                            translation_session=translation_session,
                            timeline_offset_ms=recording_session.timeline_offset_ms,
                        )
                    else:  # recording.stop
                        await _emit_events(
                            send_json,
                            transcript_service,
                            await stream.close(),
                            translation_session=translation_session,
                            timeline_offset_ms=recording_session.timeline_offset_ms,
                        )
                        stream = None
                        if translation_session is not None:
                            await translation_session.flush()
                            await translation_session.close()
                        if not await _save_recording(recording_session, meeting.id):
                            retryable_interruption = True
                            await _error_and_close(
                                websocket,
                                send_json,
                                "AUDIO_STORAGE_ERROR",
                                "The realtime recording could not be saved.",
                                True,
                            )
                            return
                        await transcript_service.recording_completed()
                        await send_json({"type": "meeting.status", "status": "completed"})
                        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
                        return
                except RealtimeRecordingError:
                    retryable_interruption = True
                    await _error_and_close(
                        websocket,
                        send_json,
                        "AUDIO_STORAGE_ERROR",
                        "The realtime recording could not be saved.",
                        True,
                    )
                    return
                except STTProviderError as exc:
                    terminal_failure = not exc.retryable
                    retryable_interruption = exc.retryable
                    if terminal_failure:
                        await transcript_service.recording_failed()
                    await _save_recording(recording_session, meeting.id)
                    await _handle_provider_error(websocket, send_json, exc)
                    return
                except ValueError as exc:
                    await _send_error(send_json, "STT_RESULT_INVALID", str(exc), False)
                except Exception:
                    logger.exception(
                        "realtime_command_failed",
                        extra={"meeting_id": str(meeting.id)},
                    )
                    retryable_interruption = True
                    await _save_recording(recording_session, meeting.id)
                    await _handle_provider_error(
                        websocket,
                        send_json,
                        STTProviderError(
                            "The STT provider could not process this realtime command."
                        ),
                    )
                    return
    except WebSocketDisconnect:
        pass
    except ApiError as exc:
        await _error_and_close(websocket, send_json, exc.code, exc.message, retryable=False)
    except Exception:
        await _error_and_close(
            websocket,
            send_json,
            "INTERNAL_SERVER_ERROR",
            "Realtime transcription could not continue.",
            False,
        )
    finally:
        if translation_session is not None:
            await translation_session.close()
        if stream is not None:
            try:
                events = await stream.close()
                if transcript_service is not None:
                    await _emit_events(
                        send_json,
                        transcript_service,
                        events,
                        send_client=False,
                        timeline_offset_ms=(
                            recording_session.timeline_offset_ms
                            if recording_session is not None
                            else 0
                        ),
                    )
            except (STTProviderError, RuntimeError, ValueError):
                pass
        if recording_session is not None:
            await _save_recording(recording_session, meeting_id)
        if transcript_service is not None and not terminal_failure and not retryable_interruption:
            try:
                await transcript_service.recording_ended()
            except RuntimeError:
                pass


async def _save_recording(
    recording_session: RealtimeRecordingService, meeting_id: UUID
) -> bool:
    try:
        await recording_session.finalize()
        return True
    except Exception:
        logger.exception(
            "realtime_recording_save_failed",
            extra={"meeting_id": str(meeting_id)},
        )
        return False


async def _flush_interrupted_stream(
    stream: STTStream,
    transcript_service: RealtimeTranscriptService,
    timeline_offset_ms: int,
) -> None:
    """Persist provider-finalized segments before closing an interrupted socket."""
    try:
        await _emit_events(
            _discard_json,
            transcript_service,
            await stream.close(),
            send_client=False,
            timeline_offset_ms=timeline_offset_ms,
        )
    except (STTProviderError, RuntimeError, ValueError):
        pass


async def _discard_json(_: dict[str, object]) -> None:
    return None


async def _websocket_user(websocket: WebSocket, settings: Settings) -> User:
    token = websocket.query_params.get("access_token")
    if not token:
        raise ApiError("AUTH_REQUIRED", "Authentication is required.", 401)
    user_id = TokenService(
        settings.jwt_secret, settings.access_token_ttl_seconds, settings.refresh_token_ttl_seconds
    ).verify(token, "access")
    database = cast(Database, websocket.app.state.database)
    async with database.session() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_email_verified:
            raise ApiError("AUTH_REQUIRED", "Authentication is required.", 401)
        # The session used for access/persistence must own the object instance.
        return User(id=user.id, email=user.email, full_name=user.full_name, is_email_verified=True)


def _command(raw_message: str) -> RealtimeCommand | None:
    try:
        payload: Any = json.loads(raw_message)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    command_type = payload.get("type")
    if command_type in {
        "recording.pause",
        "recording.resume",
        "recording.stop",
        "translation.disable",
    }:
        return RealtimeCommand(command_type)
    if command_type != "translation.enable":
        return None
    target_language = payload.get("target_language")
    logical_segment_ids = payload.get("logical_segment_ids", [])
    if (
        not isinstance(target_language, str)
        or not 2 <= len(target_language.strip()) <= 35
        or not isinstance(logical_segment_ids, list)
        or any(not isinstance(item, str) for item in logical_segment_ids)
    ):
        return None
    try:
        parsed_ids = tuple(dict.fromkeys(UUID(item) for item in logical_segment_ids))
    except ValueError:
        return None
    return RealtimeCommand(
        type="translation.enable",
        target_language=target_language.strip().lower(),
        logical_segment_ids=parsed_ids,
    )


async def _emit_events(
    send_json: SendJson,
    transcript_service: RealtimeTranscriptService,
    events: tuple[StreamingTranscriptionEvent, ...],
    *,
    translation_session: RealtimeTranslationSession | None = None,
    send_client: bool = True,
    timeline_offset_ms: int = 0,
) -> None:
    for event in events:
        segment = _offset_segment(event.segment, timeline_offset_ms)
        if event.type == "speaker_revision":
            logical_segment_id = await transcript_service.revise_speaker(
                event.provider_segment_id, segment.speaker_label
            )
            if send_client and logical_segment_id is not None:
                await send_json(
                    {
                        "type": "transcript.speaker_revision",
                        "logical_segment_id": str(logical_segment_id),
                        "speaker_label": segment.speaker_label,
                    }
                )
            continue
        payload = {
            "type": f"transcript.{event.type}",
            "language": event.language,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "text": segment.text,
            "speaker_label": segment.speaker_label,
            "confidence": segment.confidence,
        }
        logical_segment_id: UUID | None = None
        if event.type == "final":
            stored = await transcript_service.persist_final(
                segment, event.provider_segment_id
            )
            logical_segment_id = stored.logical_segment_id
            segment = TranscriptionSegment(
                start_ms=stored.start_ms,
                end_ms=stored.end_ms,
                text=stored.text,
                speaker_label=segment.speaker_label,
                confidence=stored.confidence,
            )
            payload["start_ms"] = segment.start_ms
            payload["end_ms"] = segment.end_ms
            payload["text"] = segment.text
            payload["confidence"] = segment.confidence
            payload["logical_segment_id"] = str(logical_segment_id)
        if send_client:
            await send_json({key: value for key, value in payload.items() if value is not None})
            if logical_segment_id is not None and translation_session is not None:
                translation_session.add_final(
                    RealtimeTranslationItem(logical_segment_id, segment, event.language)
                )


def _offset_segment(
    segment: TranscriptionSegment, timeline_offset_ms: int
) -> TranscriptionSegment:
    if timeline_offset_ms <= 0:
        return segment
    return TranscriptionSegment(
        start_ms=segment.start_ms + timeline_offset_ms,
        end_ms=segment.end_ms + timeline_offset_ms,
        text=segment.text,
        speaker_label=segment.speaker_label,
        confidence=segment.confidence,
    )


async def _send_error(send_json: SendJson, code: str, message: str, retryable: bool) -> None:
    await send_json(
        {"type": "error", "code": code, "message": message, "retryable": retryable}
    )


async def _error_and_close(
    websocket: WebSocket,
    send_json: SendJson,
    code: str,
    message: str,
    retryable: bool,
) -> None:
    await _send_error(send_json, code, message, retryable)
    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)


async def _handle_provider_error(
    websocket: WebSocket, send_json: SendJson, error: STTProviderError
) -> None:
    await _send_error(send_json, error.code, str(error), error.retryable)
    await websocket.close(
        code=1013 if error.retryable else status.WS_1011_INTERNAL_ERROR,
        reason=error.code,
    )


async def _send_translation_error(
    send_json: SendJson,
    code: str,
    message: str,
    retryable: bool,
    logical_segment_ids: tuple[UUID, ...] = (),
) -> None:
    await send_json(
        {
            "type": "translation.error",
            "code": code,
            "message": message,
            "retryable": retryable,
            "logical_segment_ids": [str(logical_id) for logical_id in logical_segment_ids],
        }
    )


def _opposite_language(source_language: str) -> str | None:
    return {"vi": "en", "en": "vi"}.get(source_language)


def _translation_configuration_supported(
    source_language: str,
    target_language: str,
    supported_pairs: frozenset[tuple[str, str]],
) -> bool:
    if source_language == "auto":
        return target_language == "auto" and {
            ("vi", "en"),
            ("en", "vi"),
        }.issubset(supported_pairs)
    return (
        target_language == _opposite_language(source_language)
        and (source_language, target_language) in supported_pairs
    )
