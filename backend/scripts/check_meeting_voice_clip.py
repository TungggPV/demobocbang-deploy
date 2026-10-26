"""Smoke test real FFmpeg extraction; no database, credentials or AI calls.

Run inside the backend image with current sources mounted read-only.
"""

import asyncio
import tempfile
from pathlib import Path
from uuid import uuid4

from app.models.media import MediaFile
from app.providers.storage import LocalStorageBackend
from app.services.meeting_audio import MeetingAudioReader, pcm_wav


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="voice-clip-check-") as directory:
        storage = LocalStorageBackend(Path(directory))
        first = b"\x10\x00" * (16_000 * 15)
        second = b"\x20\x00" * (16_000 * 15)
        await storage.put_bytes(pcm_wav(first + second), "input.wav")
        media = MediaFile(
            id=uuid4(),
            meeting_id=uuid4(),
            original_filename="input.wav",
            mime_type="audio/wav",
            size_bytes=len(first + second) + 44,
            duration_ms=30_000,
            storage_key="input.wav",
            status="completed",
        )
        reader = MeetingAudioReader(None, storage)
        result = await reader._file_clip(media, [(1000, 7000), (20000, 26000)])
        assert result == b"\x10\x00" * (16_000 * 6) + b"\x20\x00" * (16_000 * 6)
        print("PASS: FFmpeg extracted and joined exactly the two selected intervals (12 seconds).")


if __name__ == "__main__":
    asyncio.run(main())
