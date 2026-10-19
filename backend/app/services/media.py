"""Media upload, ffprobe validation, storage cleanup, and durable queue jobs."""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.logging import get_logger
from app.models.enums import JobStatus, MeetingPermission
from app.models.media import Job, MediaFile
from app.models.user import User
from app.providers.storage import MediaStorage, StorageError, StorageNotFoundError
from app.schemas.media import JobErrorResponse, JobResponse, MediaFileResponse
from app.services.meetings import MeetingService

_ALLOWED_EXTENSIONS = frozenset(
    {
        "mp3",
        "wav",
        "m4a",
        "aac",
        "flac",
        "ogg",
        "opus",
        "webm",
        "mp4",
        "mov",
        "mkv",
        "avi",
        "mpeg",
        "mpg",
    }
)
_UPLOAD_CHUNK_BYTES = 1024 * 1024
logger = get_logger(__name__)


class MediaService:
    def __init__(self, session: AsyncSession, settings: Settings, storage: MediaStorage) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage
        self._meetings = MeetingService(session)

    async def upload(self, meeting_id: UUID, user: User, upload: UploadFile) -> JobResponse:
        meeting, _ = await self._meetings.require_access(meeting_id, user, MeetingPermission.EDIT)
        filename, extension = self._validate_filename(upload.filename)
        self._reject_oversized_content_length(upload)
        temp_path: Path | None = None
        storage_key: str | None = None
        try:
            temp_path, size_bytes = await self._write_temporary_upload(upload)
            duration_ms = await self._probe_duration(temp_path)
            if duration_ms > self._settings.max_media_duration_seconds * 1000:
                raise ApiError(
                    "MEDIA_INVALID",
                    "Media duration exceeds the configured limit.",
                    422,
                    {"max_duration_seconds": self._settings.max_media_duration_seconds},
                )
            media_id = uuid4()
            storage_key = f"media/meetings/{meeting.id}/{media_id}.{extension}"
            await self._storage.put_file(temp_path, storage_key)
            media = MediaFile(
                id=media_id,
                meeting_id=meeting.id,
                original_filename=filename,
                mime_type=upload.content_type or "application/octet-stream",
                size_bytes=size_bytes,
                duration_ms=duration_ms,
                storage_key=storage_key,
                status="uploaded",
            )
            self._session.add(media)
            # Job only stores the media UUID, so SQLAlchemy has no mapped
            # relationship from which to derive insert ordering. Flush the
            # referenced row before adding its dependent job.
            await self._session.flush()
            job = Job(
                meeting_id=meeting.id,
                media_file_id=media.id,
                job_type="media_processing",
                status=JobStatus.QUEUED,
                progress=0,
            )
            self._session.add(job)
            await self._session.commit()
            await self._session.refresh(job)
            return self._job_response(job)
        except StorageError as exc:
            await self._session.rollback()
            if storage_key is not None:
                try:
                    await self._storage.delete(storage_key)
                except StorageError:
                    logger.warning("storage_cleanup_failed", extra={"storage_key": storage_key})
            raise ApiError("MEDIA_STORAGE_ERROR", "Media could not be stored.", 503) from exc
        except Exception:
            await self._session.rollback()
            if storage_key is not None:
                try:
                    await self._storage.delete(storage_key)
                except (OSError, StorageError):
                    logger.warning("storage_cleanup_failed", extra={"storage_key": storage_key})
            raise
        finally:
            if temp_path is not None:
                try:
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                except OSError:
                    pass
            await upload.close()

    async def list_media(self, meeting_id: UUID, user: User) -> list[MediaFileResponse]:
        await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        media = await self._session.scalars(
            select(MediaFile)
            .where(MediaFile.meeting_id == meeting_id, MediaFile.deleted_at.is_(None))
            .order_by(MediaFile.created_at.desc())
        )
        return [MediaFileResponse.model_validate(item) for item in media]

    async def read_media_range(
        self, meeting_id: UUID, media_id: UUID, user: User, start: int, end: int
    ) -> tuple[bytes, MediaFile]:
        """Return an ACL-checked inclusive byte range for browser playback/download."""
        await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        media = await self._session.get(MediaFile, media_id)
        if media is None or media.meeting_id != meeting_id or media.deleted_at is not None:
            raise ApiError("MEDIA_NOT_FOUND", "Media was not found.", 404)
        if start < 0 or end < start or start >= media.size_bytes:
            raise ApiError("MEDIA_RANGE_INVALID", "The requested media range is invalid.", 416)
        try:
            return await self._storage.get_range(media.storage_key, start, min(end, media.size_bytes - 1)), media
        except (FileNotFoundError, StorageNotFoundError) as exc:
            raise ApiError("MEDIA_UNAVAILABLE", "Media is no longer available.", 404) from exc
        except (OSError, StorageError) as exc:
            raise ApiError("MEDIA_STORAGE_ERROR", "Media could not be read.", 503) from exc

    async def media_for_playback_ticket(self, meeting_id: UUID, media_id: UUID) -> MediaFile:
        """Resolve media named by a ticket that was issued after an ACL check."""
        media = await self._session.get(MediaFile, media_id)
        if media is None or media.meeting_id != meeting_id or media.deleted_at is not None:
            raise ApiError("MEDIA_NOT_FOUND", "Media was not found.", 404)
        return media

    async def read_ticket_media_range(self, media: MediaFile, start: int, end: int) -> bytes:
        if start < 0 or end < start or start >= media.size_bytes:
            raise ApiError("MEDIA_RANGE_INVALID", "The requested media range is invalid.", 416)
        try:
            return await self._storage.get_range(media.storage_key, start, min(end, media.size_bytes - 1))
        except (FileNotFoundError, StorageNotFoundError) as exc:
            raise ApiError("MEDIA_UNAVAILABLE", "Media is no longer available.", 404) from exc
        except (OSError, StorageError) as exc:
            raise ApiError("MEDIA_STORAGE_ERROR", "Media could not be read.", 503) from exc

    async def presigned_url(
        self, meeting_id: UUID, media_id: UUID, user: User, expires_in: int, *, download: bool = False
    ) -> str | None:
        """Return a private S3 URL only after the normal meeting ACL check."""
        await self._meetings.require_access(meeting_id, user, MeetingPermission.READ)
        media = await self._session.get(MediaFile, media_id)
        if media is None or media.meeting_id != meeting_id or media.deleted_at is not None:
            raise ApiError("MEDIA_NOT_FOUND", "Media was not found.", 404)
        disposition = (
            f"attachment; filename*=UTF-8''{quote(media.original_filename, safe='')}"
            if download
            else None
        )
        try:
            return await self._storage.presigned_get_url(
                media.storage_key,
                expires_in,
                content_type=media.mime_type,
                content_disposition=disposition,
            )
        except StorageError as exc:
            raise ApiError("MEDIA_STORAGE_ERROR", "Media URL could not be created.", 503) from exc

    async def delete_media(self, meeting_id: UUID, media_id: UUID, user: User) -> None:
        await self._meetings.require_access(meeting_id, user, "owner")
        media = await self._session.get(MediaFile, media_id)
        if media is None or media.meeting_id != meeting_id or media.deleted_at is not None:
            raise ApiError("MEDIA_NOT_FOUND", "Media was not found.", 404)
        keys = [media.storage_key]
        if media.normalized_storage_key is not None:
            keys.append(media.normalized_storage_key)
        try:
            await self._storage.delete_many(keys)
        except (OSError, StorageError) as exc:
            raise ApiError("MEDIA_STORAGE_ERROR", "Media could not be deleted.", 502) from exc
        pending_jobs = await self._session.scalars(
            select(Job).where(
                Job.media_file_id == media.id,
                Job.status.in_((JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.RETRYING)),
            )
        )
        for job in pending_jobs:
            job.status = JobStatus.FAILED
            job.error_code = "MEDIA_DELETED"
            job.error_message = "The source media was deleted before processing finished."
        media.deleted_at = _now()
        media.status = "deleted"
        await self._session.commit()

    async def purge_meeting_storage(self, meeting_id: UUID) -> None:
        """Remove every object before the owning meeting is deleted from the DB."""
        media_files = await self._session.scalars(
            select(MediaFile).where(MediaFile.meeting_id == meeting_id)
        )
        keys: list[str] = []
        for media in media_files:
            if media.storage_key:
                keys.append(media.storage_key)
            if media.normalized_storage_key:
                keys.append(media.normalized_storage_key)
        if keys:
            try:
                await self._storage.delete_many(keys)
            except (OSError, StorageError) as exc:
                raise ApiError(
                    "MEDIA_STORAGE_ERROR", "Meeting media could not be deleted.", 502
                ) from exc

    async def get_job(self, job_id: UUID, user: User) -> JobResponse:
        job = await self._session.get(Job, job_id)
        if job is None:
            raise ApiError("JOB_NOT_FOUND", "Job was not found.", 404)
        await self._meetings.require_access(job.meeting_id, user, MeetingPermission.READ)
        return self._job_response(job)

    def _validate_filename(self, original: str | None) -> tuple[str, str]:
        if original is None or not original.strip():
            raise ApiError("MEDIA_UNSUPPORTED", "A media filename is required.", 422)
        filename = Path(original).name.strip()
        extension = Path(filename).suffix.lower().removeprefix(".")
        if extension not in _ALLOWED_EXTENSIONS:
            raise ApiError("MEDIA_UNSUPPORTED", "This media format is not supported.", 422)
        return filename[:512], extension

    def _reject_oversized_content_length(self, upload: UploadFile) -> None:
        content_length = upload.headers.get("content-length")
        if content_length is None:
            return
        try:
            exceeds_limit = int(content_length) > self._settings.max_upload_bytes
        except ValueError:
            return
        if exceeds_limit:
            raise ApiError(
                "UPLOAD_TOO_LARGE",
                "Upload exceeds the configured size limit.",
                413,
                {"max_upload_bytes": self._settings.max_upload_bytes},
            )

    async def _write_temporary_upload(self, upload: UploadFile) -> tuple[Path, int]:
        descriptor, raw_path = tempfile.mkstemp(prefix="bocbang-media-", suffix=".upload")
        path = Path(raw_path)
        size_bytes = 0
        try:
            with os.fdopen(descriptor, "wb") as destination:
                while chunk := await upload.read(_UPLOAD_CHUNK_BYTES):
                    size_bytes += len(chunk)
                    if size_bytes > self._settings.max_upload_bytes:
                        raise ApiError(
                            "UPLOAD_TOO_LARGE",
                            "Upload exceeds the configured size limit.",
                            413,
                            {"max_upload_bytes": self._settings.max_upload_bytes},
                        )
                    destination.write(chunk)
        except Exception:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            raise
        if size_bytes == 0:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            raise ApiError("MEDIA_INVALID", "Uploaded media is empty.", 422)
        return path, size_bytes

    async def _probe_duration(self, path: Path) -> int:
        try:
            # Uvicorn's Windows reload process uses SelectorEventLoop, which
            # does not implement asyncio subprocess transports.  Running the
            # short probe in a worker thread keeps local development and
            # production behavior consistent.
            process = await asyncio.to_thread(
                subprocess.run,
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    str(path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ApiError(
                "MEDIA_PROBE_UNAVAILABLE", "Media validation is not configured.", 503
            ) from exc
        if process.returncode != 0:
            raise ApiError("MEDIA_INVALID", "Media cannot be read by ffprobe.", 422)
        try:
            duration_seconds = float(json.loads(process.stdout).get("format", {}).get("duration"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ApiError("MEDIA_INVALID", "Media has no readable duration.", 422) from exc
        if not math.isfinite(duration_seconds):
            raise ApiError("MEDIA_INVALID", "Media has no readable duration.", 422)
        duration_ms = round(duration_seconds * 1000)
        if duration_ms <= 0:
            raise ApiError("MEDIA_INVALID", "Media has no readable duration.", 422)
        return duration_ms

    @staticmethod
    def _job_response(job: Job) -> JobResponse:
        error = (
            JobErrorResponse(
                code=job.error_code,
                message=job.error_message,
                retryable=job.error_retryable,
            )
            if job.error_code is not None and job.error_message is not None
            else None
        )
        return JobResponse(
            id=job.id,
            meeting_id=job.meeting_id,
            media_file_id=job.media_file_id,
            job_type=job.job_type,
            status=job.status,
            progress=job.progress,
            error=error,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


def _now() -> datetime:
    return datetime.now(UTC)
