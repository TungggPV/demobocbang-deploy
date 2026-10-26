"""Authenticated realtime STT WebSocket endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any, cast
from uuid import UUID

from anyio import CancelScope
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.security import TokenService
from app.db.session import Database
from app.models.enums import MeetingPermission, MeetingStatus
from app.models.user import User
from app.providers.contracts import (
    StreamingEvent,
    StreamingOptions,
    StreamingSpeakerRevision,
    STTProvider,
    STTProviderError,
    STTStream,
    TranslationProvider,
)
from app.services.meeting_audio import RealtimeAudioWriter
from app.services.meeting_sections import MeetingSectionService
from app.services.meetings import MeetingService
from app.services.realtime import RealtimeTranscriptService
from app.services.realtime_translation import (
    RealtimeTranslationItem,
    RealtimeTranslationSession,
)
from app.services.voice_profiles import VoiceProfileService

router = APIRouter()
SendJson = Callable[[dict[str, object]], Awaitable[None]]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RealtimeCommand:
    type: str
    new_section: bool = False
    acknowledge: bool = False
    target_language: str | None = None
    logical_segment_ids: tuple[UUID, ...] = ()


@router.websocket("/realtime/{meeting_id}")
async def realtime_transcription(websocket: WebSocket, meeting_id: UUID) -> None:
    """Accept binary audio and emit normalized partial/final transcript events."""
    await websocket.accept()
    stream: STTStream | None = None
    transcript_service: RealtimeTranscriptService | None = None
    translation_session: RealtimeTranslationSession | None = None
    audio_writer: RealtimeAudioWriter | None = None
    send_lock = asyncio.Lock()
    session_stack = AsyncExitStack()

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
        session = await session_stack.enter_async_context(database.session())
        meeting, _ = await MeetingService(session).require_access(
            meeting_id, user, MeetingPermission.EDIT
        )
        start_paused = websocket.query_params.get("start_paused") == "1"
        if meeting.status == MeetingStatus.COMPLETED:
            raise ApiError("MEETING_COMPLETED", "This meeting has already ended.", 409)
        known_speakers = await VoiceProfileService(session).list_known_speakers(
            meeting_id
        )
        options = StreamingOptions(
            language=meeting.language_mode,
            diarization=meeting.diarization_enabled,
            known_speakers=known_speakers,
        )
        if options.diarization and not provider.capabilities().diarization:
            await _error_and_close(
                websocket, send_json, "PROVIDER_CAPABILITY_UNAVAILABLE",
                "The configured STT provider does not support diarization.", False,
            )
            return
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
        transcript_service = RealtimeTranscriptService(session, meeting, user.id)
        audio_writer = RealtimeAudioWriter(
            session, websocket.app.state.media_storage, meeting.id,
            transcript_service.time_offset_ms, settings.max_media_duration_seconds * 1000,
        )
        if translation_provider is not None:
            translation_session = RealtimeTranslationSession(
                translation_provider,
                meeting.language_mode,
                send_json,
            )
        if start_paused:
            await session.refresh(meeting, with_for_update=True)
            await MeetingSectionService(session).ensure(meeting)
            await session.commit()
            await stream.pause()
        else:
            await transcript_service.recording_started()
        await send_json(
            {
                "type": "session.ready",
                "paused": start_paused,
                "provider": getattr(
                    stream,
                    "provider_name",
                    settings.stt_provider or provider.__class__.__name__,
                ),
                "language": meeting.language_mode,
                "speaker_identification_available": (
                    getattr(stream, "speaker_identification_available", False)
                ),
                "fallback_active": getattr(stream, "fallback_active", False),
            }
        )
        paused = start_paused
        split_pending = start_paused
        while True:
            try:
                message = (await _receive_with_events(
                    websocket, stream, send_json, transcript_service, translation_session
                )) if stream is not None else await websocket.receive()
            except STTProviderError as exc:
                if not exc.retryable:
                    await transcript_service.recording_failed()
                await _handle_provider_error(websocket, send_json, exc)
                return
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
                    await audio_writer.append(message["bytes"])
                    events = await stream.send_audio(message["bytes"])
                    await _emit_events(
                        send_json,
                        transcript_service,
                        events,
                        translation_session=translation_session,
                    )
                except ApiError as exc:
                    await _error_and_close(websocket, send_json, exc.code, exc.message, False)
                    return
                except STTProviderError as exc:
                    if not exc.retryable:
                        await transcript_service.recording_failed()
                    await _handle_provider_error(websocket, send_json, exc)
                    return
                except ValueError as exc:
                    await _send_error(send_json, "STT_RESULT_INVALID", str(exc), False)
                except Exception:
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
                supported_target = _opposite_language(meeting.language_mode)
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
                if (
                    target != supported_target
                    or (meeting.language_mode, target)
                    not in translation_provider.capabilities().supported_language_pairs
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
                if command.type == "recording.pause" and command.new_section:
                    if not paused:
                        await _emit_events(send_json, transcript_service, await stream.close(),
                                           translation_session=translation_session)
                        stream = None
                        await audio_writer.flush()
                        boundary = max(audio_writer.end_ms, meeting.duration_ms or 0)
                        next_section = await MeetingSectionService(session).split(meeting, boundary)
                        boundary = next_section.start_ms
                        await session.commit()
                        # The next upstream and retained audio use the same new clock.
                        audio_writer = RealtimeAudioWriter(session, websocket.app.state.media_storage,
                            meeting.id, boundary, settings.max_media_duration_seconds * 1000)
                    paused, split_pending = True, True
                    await send_json({"type": "meeting.sections", "paused": True,
                        "sections": [row.model_dump(mode="json") for row in await MeetingSectionService(session).list(meeting_id, user)]})
                elif command.type == "speakers.refresh":
                    if stream is None:
                        await send_json({"type": "speakers.updated", "speaker_identification_available": False, "fallback_active": False, "deferred": True})
                        continue
                    await _emit_events(
                        send_json, transcript_service, await stream.close(),
                        translation_session=translation_session,
                    )
                    stream = None
                    await audio_writer.flush()
                    # The replacement provider sees the new voiceprints. Its local
                    # timestamps start at zero; the retained PCM clock supplies the
                    # meeting offset, including silence and earlier pauses.
                    known_speakers = await VoiceProfileService(session).list_known_speakers(meeting_id)
                    stream = await provider.open_stream(replace(options, known_speakers=known_speakers))
                    transcript_service = RealtimeTranscriptService(
                        session, meeting, user.id, time_offset_ms=audio_writer.end_ms,
                    )
                    if paused:
                        await stream.pause()
                    await send_json({
                        "type": "speakers.updated",
                        "speaker_identification_available": getattr(stream, "speaker_identification_available", False),
                        "fallback_active": getattr(stream, "fallback_active", False),
                    })
                elif command.type == "recording.pause":
                    await audio_writer.flush()
                    paused = True
                    if stream is not None:
                        await _emit_events(
                            send_json,
                            transcript_service,
                            await stream.pause(),
                            translation_session=translation_session,
                        )
                    if command.acknowledge:
                        await send_json({"type": "recording.paused"})
                elif command.type == "recording.resume":
                    resuming_new_section = not (await MeetingSectionService(session).ensure(meeting)).is_started
                    if stream is None:
                        known_speakers = await VoiceProfileService(session).list_known_speakers(meeting_id)
                        stream = await provider.open_stream(replace(options, known_speakers=known_speakers))
                        transcript_service = RealtimeTranscriptService(session, meeting, user.id,
                            time_offset_ms=audio_writer.end_ms)
                    await transcript_service.recording_started()
                    paused = False
                    await _emit_events(
                        send_json,
                        transcript_service,
                        await stream.resume(),
                        translation_session=translation_session,
                    )
                    if split_pending or command.acknowledge:
                        await send_json({
                            "type": "recording.resumed",
                            "new_section": resuming_new_section,
                            "speaker_identification_available": getattr(stream, "speaker_identification_available", False),
                            "fallback_active": getattr(stream, "fallback_active", False),
                        })
                        split_pending = False
                else:  # recording.stop
                    await _emit_events(
                        send_json,
                        transcript_service,
                        await stream.close() if stream is not None else (),
                        translation_session=translation_session,
                    )
                    stream = None
                    await audio_writer.flush()
                    if translation_session is not None:
                        await translation_session.flush()
                        await translation_session.close()
                    from app.services.transcript_polish import complete_with_polish
                    await complete_with_polish(session, meeting_id, user, websocket.app.state)
                    await send_json({"type": "meeting.status", "status": "completed"})
                    await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
                    return
            except STTProviderError as exc:
                if not exc.retryable:
                    await transcript_service.recording_failed()
                await _handle_provider_error(websocket, send_json, exc)
                return
            except ValueError as exc:
                await _send_error(send_json, "STT_RESULT_INVALID", str(exc), False)
            except ApiError as exc:
                await _error_and_close(websocket, send_json, exc.code, exc.message, False)
                return
            except Exception:
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
        with CancelScope(shield=True):
            try:
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
                            )
                    except (STTProviderError, RuntimeError, ValueError):
                        pass
                # Closing a browser connection is an interruption, not an explicit
                # request to finish the meeting.  Keep the meeting resumable; only
                # the recording.stop command (or the complete HTTP endpoint) may
                # move it to COMPLETED.
            finally:
                if audio_writer is not None:
                    try:
                        await audio_writer.flush()
                    except Exception:
                        logger.exception("realtime_audio_flush_failed")
                await session_stack.aclose()


async def _receive_with_events(
    websocket: WebSocket,
    stream: STTStream,
    send_json: SendJson,
    transcript_service: RealtimeTranscriptService,
    translation_session: RealtimeTranslationSession | None,
) -> dict[str, Any]:
    """Multiplex audio/control and provider output with one serialized DB writer.

    No reader remains alive during pause/close, so it cannot steal the final flush.
    Legacy injected streams can continue returning events from send_audio().
    """
    receive_events = getattr(stream, "receive_events", None)
    if receive_events is None:
        return await websocket.receive()
    message_task = asyncio.create_task(websocket.receive())
    event_task = None
    try:
        while True:
            event_task = asyncio.create_task(receive_events())
            done, _ = await asyncio.wait(
                (message_task, event_task), return_when=asyncio.FIRST_COMPLETED
            )
            if event_task in done:
                await _emit_events(
                    send_json, transcript_service, event_task.result(),
                    translation_session=translation_session,
                )
            if message_task in done:
                return message_task.result()
    finally:
        tasks = [task for task in (message_task, event_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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
        "speakers.refresh",
        "translation.disable",
    }:
        new_section = payload.get("new_section", False)
        acknowledge = payload.get("acknowledge", False)
        if not isinstance(new_section, bool) or not isinstance(acknowledge, bool):
            return None
        return RealtimeCommand(command_type, new_section=new_section, acknowledge=acknowledge)
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
    events: tuple[StreamingEvent, ...],
    *,
    translation_session: RealtimeTranslationSession | None = None,
    send_client: bool = True,
) -> None:
    for event in events:
        if isinstance(event, StreamingSpeakerRevision):
            mutation = await transcript_service.revise_speaker(
                event.turn_order, event.speaker_label
            )
            if mutation is not None and mutation.segment is not None and send_client:
                await send_json({
                    "type": "transcript.speaker_updated",
                    "logical_segment_id": str(mutation.segment.logical_segment_id),
                    "speaker_id": str(mutation.segment.speaker_id)
                    if mutation.segment.speaker_id else None,
                    "version_id": str(mutation.version_id),
                    "version_no": mutation.version_no,
                })
            continue
        segment = event.segment
        payload = {
            "type": f"transcript.{event.type}",
            "language": event.language,
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "text": segment.text,
            "speaker_label": segment.speaker_label
            if transcript_service.diarization_enabled else None,
            "speaker_name": segment.speaker_name
            if transcript_service.diarization_enabled else None,
            "voice_profile_id": str(segment.voice_profile_id)
            if segment.voice_profile_id and transcript_service.diarization_enabled
            else None,
            "is_identified": bool(
                segment.is_identified and transcript_service.diarization_enabled
            ),
            "confidence": segment.confidence,
            "event_id": event.event_id,
            "stream_segment_id": transcript_service.stream_session_id + ":" + (
                event.stream_segment_id or (
                    f"turn:{event.turn_order}" if event.turn_order is not None
                    else f"{segment.start_ms}:{segment.speaker_label or ''}"
                )
            ),
            "stream_start_ms": segment.start_ms,
            "stream_end_ms": segment.end_ms,
        }
        logical_segment_id: UUID | None = None
        if event.type == "partial":
            payload["start_ms"] = segment.start_ms + transcript_service.time_offset_ms
            payload["end_ms"] = segment.end_ms + transcript_service.time_offset_ms
        if event.type == "final":
            stored = await transcript_service.persist_final(segment, event.turn_order)
            logical_segment_id = stored.logical_segment_id
            payload["logical_segment_id"] = str(logical_segment_id)
            payload["speaker_id"] = str(stored.speaker_id) if stored.speaker_id else None
            payload["start_ms"], payload["end_ms"] = stored.start_ms, stored.end_ms
            segment = replace(segment, start_ms=stored.start_ms, end_ms=stored.end_ms)
        if send_client:
            payload["backend_queue_ms"] = round((perf_counter() - event.captured_at) * 1000, 2)
            logger.debug(
                "STT event=%s type=%s segment=%s backend_queue_ms=%s",
                event.event_id, event.type, payload["stream_segment_id"],
                payload["backend_queue_ms"],
            )
            await send_json({key: value for key, value in payload.items() if value is not None})
            if logical_segment_id is not None and translation_session is not None:
                translation_session.add_final(
                    RealtimeTranslationItem(logical_segment_id, segment)
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
