"""Authenticated endpoints for user and meeting guest voice profiles."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status

from app.api.v1.routes.auth import CurrentUserDependency
from app.core.dependencies import DatabaseSessionDependency, get_database, get_settings
from app.core.errors import ApiError
from app.schemas.media import JobResponse
from app.schemas.voice_profile import (
    MeetingVoiceLearningRequest,
    MeetingVoiceSampleRequest,
    VoiceProfileResponse,
)
from app.services.voice_learning import MeetingVoiceLearningService
from app.services.voice_profiles import VoiceProfileService
from app.workers.lifecycle import fail_enqueue

voice_profiles_router = APIRouter(prefix="/voice-profiles")
meeting_voice_profiles_router = APIRouter(prefix="/meetings")


async def get_voice_profile_service(
    request: Request,
    session: DatabaseSessionDependency,
) -> VoiceProfileService:
    return VoiceProfileService(
        session,
        request.app.state.speaker_enrollment_provider,
        getattr(request.app.state, "media_storage", None),
    )


VoiceProfileServiceDependency = Annotated[VoiceProfileService, Depends(get_voice_profile_service)]


@meeting_voice_profiles_router.post("/{meeting_id}/voice-learning/preview")
async def preview_meeting_voice_sample(
    meeting_id: UUID,
    payload: MeetingVoiceSampleRequest,
    user: CurrentUserDependency,
    request: Request,
    session: DatabaseSessionDependency,
) -> Response:
    service = MeetingVoiceLearningService(session, request.app.state.media_storage)
    meeting, segments = await service.snapshot(meeting_id, user, payload.segment_ids, edit=False)
    audio = await service.audio(meeting, segments)
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@meeting_voice_profiles_router.post(
    "/{meeting_id}/voice-learning",
    response_model=JobResponse,
    status_code=202,
)
async def learn_meeting_voice(
    meeting_id: UUID,
    payload: MeetingVoiceLearningRequest,
    user: CurrentUserDependency,
    request: Request,
    session: DatabaseSessionDependency,
) -> JobResponse:
    if request.app.state.speaker_enrollment_provider is None:
        raise ApiError(
            "PROVIDER_CAPABILITY_UNAVAILABLE", "Chưa cấu hình tạo mẫu giọng Speechmatics.", 503
        )
    job = await MeetingVoiceLearningService(session, request.app.state.media_storage).request(
        meeting_id,
        user,
        payload,
    )
    try:
        request.app.state.enqueue_job(job.id, get_settings(request))
    except Exception:
        await fail_enqueue(get_database(request), job.id)
        # Keep the job visible and pollable; the reconciler will publish it again.
    return job


@voice_profiles_router.get("", response_model=list[VoiceProfileResponse])
async def list_voice_profiles(
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
) -> list[VoiceProfileResponse]:
    profiles = await service.list_user_profiles(user)
    return [VoiceProfileResponse.model_validate(p) for p in profiles]


@voice_profiles_router.post(
    "", response_model=VoiceProfileResponse, status_code=status.HTTP_201_CREATED
)
async def create_voice_profile(
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
    name: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> VoiceProfileResponse:
    content = await file.read()
    profile = await service.create_user_speaker_profile(user, name, content, file.filename)
    return VoiceProfileResponse.model_validate(profile)


@voice_profiles_router.get("/me", response_model=VoiceProfileResponse)
async def get_my_voice_profile(
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
) -> VoiceProfileResponse:
    profile = await service.get_user_profile(user)
    if profile is None:
        raise ApiError("VOICE_PROFILE_NOT_FOUND", "Chưa đăng ký mẫu giọng cá nhân.", 404)
    return VoiceProfileResponse.model_validate(profile)


@voice_profiles_router.post(
    "/me", response_model=VoiceProfileResponse, status_code=status.HTTP_201_CREATED
)
async def enroll_my_voice_profile(
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
    file: Annotated[UploadFile, File()],
) -> VoiceProfileResponse:
    content = await file.read()
    profile = await service.enroll_user_profile(user, content, file.filename)
    return VoiceProfileResponse.model_validate(profile)


@voice_profiles_router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_voice_profile(
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
) -> Response:
    await service.delete_user_profile(user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@voice_profiles_router.get("/{profile_id}/audio")
@voice_profiles_router.get("/{profile_id}/samples/{sample_index}/audio")
async def get_voice_profile_audio(
    profile_id: UUID,
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
    sample_index: int = 1,
) -> Response:
    audio_bytes, filename = await service.get_profile_audio(profile_id, user, sample_index)
    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(audio_bytes)),
            "Content-Disposition": f'inline; filename="{quote(filename)}"',
            "Cache-Control": "private, max-age=3600",
        },
    )


@voice_profiles_router.post("/{profile_id}/samples", response_model=VoiceProfileResponse)
async def add_voice_sample(
    profile_id: UUID,
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
    file: Annotated[UploadFile, File()],
) -> VoiceProfileResponse:
    content = await file.read()
    profile = await service.add_profile_sample(profile_id, user, content, file.filename)
    return VoiceProfileResponse.model_validate(profile)


@voice_profiles_router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_voice_profile(
    profile_id: UUID,
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
) -> Response:
    await service.delete_profile(profile_id, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@voice_profiles_router.delete(
    "/{profile_id}/samples/{sample_index}", response_model=VoiceProfileResponse
)
async def delete_voice_sample(
    profile_id: UUID,
    sample_index: int,
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
) -> VoiceProfileResponse:
    profile = await service.delete_profile_sample(profile_id, sample_index, user)
    return VoiceProfileResponse.model_validate(profile)


@meeting_voice_profiles_router.get(
    "/{meeting_id}/voice-profiles", response_model=list[VoiceProfileResponse]
)
async def list_meeting_voice_profiles(
    meeting_id: UUID,
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
) -> list[VoiceProfileResponse]:
    profiles = await service.list_profiles_for_meeting(meeting_id, user)
    return [VoiceProfileResponse.model_validate(p) for p in profiles]


@meeting_voice_profiles_router.post(
    "/{meeting_id}/voice-profiles",
    response_model=VoiceProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def enroll_meeting_guest_voice_profile(
    meeting_id: UUID,
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
    name: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> VoiceProfileResponse:
    content = await file.read()
    profile = await service.enroll_meeting_guest(meeting_id, user, name, content, file.filename)
    return VoiceProfileResponse.model_validate(profile)


@meeting_voice_profiles_router.post(
    "/{meeting_id}/voice-profiles/{profile_id}/samples",
    response_model=VoiceProfileResponse,
)
async def add_meeting_guest_voice_sample(
    meeting_id: UUID,
    profile_id: UUID,
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
    file: Annotated[UploadFile, File()],
) -> VoiceProfileResponse:
    profile = await service.add_meeting_guest_sample(
        meeting_id, profile_id, user, await file.read(), file.filename
    )
    return VoiceProfileResponse.model_validate(profile)


@meeting_voice_profiles_router.delete(
    "/{meeting_id}/voice-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_meeting_guest_voice_profile(
    meeting_id: UUID,
    profile_id: UUID,
    user: CurrentUserDependency,
    service: VoiceProfileServiceDependency,
) -> Response:
    await service.delete_meeting_guest(meeting_id, profile_id, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
