"""Durable worker for normalization and batch transcription jobs."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import wave
from array import array
from pathlib import Path
from uuid import UUID

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.session import Database
from app.models.enums import MeetingStatus
from app.models.media import Job, MediaFile
from app.models.meeting import Meeting
from app.providers.contracts import BatchTranscriptionOptions, STTProvider, STTProviderError
from app.providers.storage import MediaStorage
from app.services.transcripts import TranscriptService
from app.workers import lifecycle


class MediaProcessingWorker:
    """Media/STT runner invoked only by a dedicated queue worker."""

    def __init__(self, database: Database, storage: MediaStorage, stt: STTProvider | None, settings: Settings) -> None:
        self._database, self._storage, self._stt, self._settings = database, storage, stt, settings

    async def run(self, job_id: UUID) -> bool:
        """Return whether Dramatiq should retry this persisted execution."""
        async with self._database.session() as session:
            job = await session.get(Job, job_id)
            if job is None or job.job_type != "media_processing" or not await lifecycle.start(session, job):
                return False
            media = await session.get(MediaFile, job.media_file_id)
            meeting = await session.get(Meeting, job.meeting_id)
            if media is None or meeting is None or media.deleted_at is not None:
                await self._fail(session, job, "MEDIA_DELETED", "The source media was deleted before processing.", False)
                return False
            if self._stt is None or not self._stt.capabilities().batch:
                await self._fail(session, job, "PROVIDER_CAPABILITY_UNAVAILABLE", "Batch transcription is not configured.", False)
                return False
            media.status, meeting.status = "processing", MeetingStatus.PROCESSING
            await session.commit()
            source: Path | None = None
            normalized: Path | None = None
            try:
                if media.normalized_storage_key:
                    normalized = await self._download_normalized(media.normalized_storage_key)
                else:
                    source, normalized = await self._normalize(media)
                    normalized_key = f"processed/meetings/{meeting.id}/{media.id}.wav"
                    await self._storage.put_file(normalized, normalized_key)
                    media.normalized_storage_key, job.progress = normalized_key, 45
                    await session.commit()
                if not await asyncio.to_thread(_has_audible_pcm, normalized):
                    raise STTProviderError(
                        "The uploaded media contains a silent audio track.",
                        code="MEDIA_NO_AUDIBLE_AUDIO",
                        retryable=False,
                    )
                result = await self._stt.transcribe_file(
                    normalized,
                    BatchTranscriptionOptions(
                        language=meeting.language_mode,
                        diarization=meeting.diarization_enabled,
                        expected_speakers=meeting.expected_speaker_count,
                        context=". ".join(
                            item.strip()
                            for item in (meeting.title, meeting.description or "")
                            if item.strip()
                        ),
                        keyterms=(meeting.title,),
                    ),
                )
                if not result.segments:
                    raise ValueError("STT provider returned no transcript segments.")
                await TranscriptService(session).replace_stt_batch(
                    meeting, meeting.owner_id, result.segments
                )
                meeting.duration_ms = media.duration_ms
                # Processing the uploaded file does not finish the meeting. It
                # stays in the same editable state as an active recording until
                # the user is done correcting its transcript.
                meeting.status, media.status = MeetingStatus.RECORDING, "completed"
                await session.commit()
                await lifecycle.completed(session, job)
                return False
            except STTProviderError as exc:
                return await self._fail(session, job, exc.code, str(exc), exc.retryable)
            except ValueError as exc:
                return await self._fail(session, job, "MEDIA_PROCESSING_FAILED", str(exc), False)
            except Exception:
                return await self._fail(session, job, "MEDIA_PROCESSING_FAILED", "Media processing could not complete.", True)
            finally:
                for path in (source, normalized):
                    if path:
                        try:
                            await asyncio.to_thread(path.unlink, missing_ok=True)
                        except OSError:
                            pass

    async def _normalize(self, media: MediaFile) -> tuple[Path, Path]:
        source_fd, source_name = tempfile.mkstemp(prefix="bocbang-source-", suffix=Path(media.original_filename).suffix)
        normalized_fd, normalized_name = tempfile.mkstemp(prefix="bocbang-normalized-", suffix=".wav")
        source, normalized = Path(source_name), Path(normalized_name)
        try:
            os.close(source_fd)
            await self._storage.download_file(media.storage_key, source)
            os.close(normalized_fd)
            # See MediaService._probe_duration: Windows reload mode uses an
            # event loop without asyncio subprocess support.
            process = await asyncio.to_thread(
                subprocess.run,
                [
                    "ffmpeg", "-y", "-v", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(normalized),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if process.returncode != 0 or not normalized.is_file() or normalized.stat().st_size == 0:
                raise ValueError(f"FFmpeg could not normalize the media: {process.stderr.decode(errors='replace')[:200]}")
            return source, normalized
        except Exception:
            for path in (source, normalized):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    async def _download_normalized(self, key: str) -> Path:
        descriptor, name = tempfile.mkstemp(prefix="bocbang-normalized-", suffix=".wav")
        path = Path(name)
        try:
            os.close(descriptor)
            await self._storage.download_file(key, path)
            return path
        except Exception:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    async def _fail(self, session: AsyncSession, job: Job, code: str, message: str, retryable: bool) -> bool:
        job_id = (inspect(job).identity or (job.id,))[0]
        await session.rollback()
        job = await session.get(Job, job_id)
        assert job is not None
        media = await session.get(MediaFile, job.media_file_id)
        meeting = await session.get(Meeting, job.meeting_id)
        if media is not None and media.deleted_at is None:
            media.status = "failed"
        if meeting is not None:
            meeting.status = MeetingStatus.FAILED
        await session.commit()
        if retryable and job.attempts <= self._settings.worker_max_retries:
            await lifecycle.retrying(
                session,
                job,
                code,
                message,
                min_backoff_ms=self._settings.worker_retry_min_backoff_ms,
                max_backoff_ms=self._settings.worker_retry_max_backoff_ms,
            )
            return True
        await lifecycle.failed(session, job, code, message, retryable)
        return False


def _has_audible_pcm(source: Path, minimum_peak: int = 8) -> bool:
    """Reject effectively digital-silent PCM16 WAV files before a paid STT call."""
    try:
        with wave.open(str(source), "rb") as audio:
            if audio.getsampwidth() != 2:
                raise ValueError("Normalized media must be PCM16 WAV.")
            while chunk := audio.readframes(16_000):
                samples = array("h")
                samples.frombytes(chunk)
                if samples and max(abs(sample) for sample in samples) >= minimum_peak:
                    return True
    except (EOFError, wave.Error) as exc:
        raise ValueError("Normalized media is not a valid PCM WAV file.") from exc
    return False
