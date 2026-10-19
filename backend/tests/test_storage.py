import asyncio
from pathlib import Path

import pytest

from app.core.config import Settings
from app.providers.storage import LocalStorageBackend, S3StorageBackend, StorageError, build_storage_backend


def test_local_storage_put_read_download_and_delete(tmp_path: Path) -> None:
    async def run() -> None:
        storage = LocalStorageBackend(tmp_path / "objects")
        source = tmp_path / "source.bin"
        source.write_bytes(b"abcdef")
        await storage.put_file(source, "media/meetings/a/source.bin")
        assert await storage.get_bytes("media/meetings/a/source.bin") == b"abcdef"
        assert await storage.get_range("media/meetings/a/source.bin", 1, 3) == b"bcd"
        destination = tmp_path / "download.bin"
        await storage.download_file("media/meetings/a/source.bin", destination)
        assert destination.read_bytes() == b"abcdef"
        await storage.delete("media/meetings/a/source.bin")
        with pytest.raises(StorageError):
            await storage.get_bytes("media/meetings/a/source.bin")
    asyncio.run(run())


def test_storage_factory_selects_local_and_rejects_incomplete_s3(tmp_path: Path) -> None:
    assert isinstance(build_storage_backend(Settings(_env_file=None, media_storage_path=tmp_path)), LocalStorageBackend)
    with pytest.raises(ValueError, match="S3_ACCESS_KEY_ID"):
        build_storage_backend(Settings(_env_file=None, storage_backend="s3"))


def test_s3_adapter_uses_bucket_key_and_presigned_url() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

        def put_object(self, *args: object, **kwargs: object) -> None:
            self.calls.append(("put_object", args, kwargs))

        def upload_file(self, *args: object, **kwargs: object) -> None:
            self.calls.append(("upload_file", args, kwargs))

        def delete_object(self, *args: object, **kwargs: object) -> None:
            self.calls.append(("delete_object", args, kwargs))

        def generate_presigned_url(self, *args: object, **kwargs: object) -> str:
            self.calls.append(("generate_presigned_url", args, kwargs))
            return "https://storage.example/signed"

    backend = S3StorageBackend.__new__(S3StorageBackend)
    backend._bucket_name = "private-bucket"
    backend._client = Client()

    source = Path("input.wav")

    async def run() -> None:
        await backend.put_file(source, "media/meetings/a/input.wav")
        await backend.put_bytes(b"body", "exports/meetings/a/file.txt")
        await backend.delete("exports/meetings/a/file.txt")
        assert await backend.presigned_get_url("exports/meetings/a/file.txt", 60) == "https://storage.example/signed"

    asyncio.run(run())
    calls = backend._client.calls
    assert calls[0][1] == ("input.wav", "private-bucket", "media/meetings/a/input.wav")
    assert calls[1][2] == {"Bucket": "private-bucket", "Key": "exports/meetings/a/file.txt", "Body": b"body"}
    assert calls[2][2] == {"Bucket": "private-bucket", "Key": "exports/meetings/a/file.txt"}
    assert calls[3][1] == ("get_object",)
    assert calls[3][2]["Params"] == {"Bucket": "private-bucket", "Key": "exports/meetings/a/file.txt"}


def test_s3_adapter_propagates_client_failure() -> None:
    class Client:
        def delete_object(self, **_: object) -> None:
            raise RuntimeError("connection failed")

    backend = S3StorageBackend.__new__(S3StorageBackend)
    backend._bucket_name = "private-bucket"
    backend._client = Client()

    with pytest.raises(StorageError, match="S3-compatible storage operation failed"):
        asyncio.run(backend.delete("media/meetings/a/file.mp3"))


def test_local_storage_delete_many(tmp_path: Path) -> None:
    async def run() -> None:
        storage = LocalStorageBackend(tmp_path / "objects")
        await storage.put_bytes(b"123", "item1.txt")
        await storage.put_bytes(b"456", "item2.txt")
        assert await storage.get_bytes("item1.txt") == b"123"
        assert await storage.get_bytes("item2.txt") == b"456"
        await storage.delete_many(["item1.txt", "item2.txt"])
        with pytest.raises(StorageError):
            await storage.get_bytes("item1.txt")
        with pytest.raises(StorageError):
            await storage.get_bytes("item2.txt")

    asyncio.run(run())


def test_s3_adapter_delete_many() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def delete_objects(self, **kwargs: object) -> None:
            self.calls.append(("delete_objects", kwargs))

    backend = S3StorageBackend.__new__(S3StorageBackend)
    backend._bucket_name = "private-bucket"
    backend._client = Client()

    async def run() -> None:
        await backend.delete_many(["file1.wav", "file2.wav"])

    asyncio.run(run())
    assert len(backend._client.calls) == 1
    call = backend._client.calls[0]
    assert call[1]["Bucket"] == "private-bucket"
    assert call[1]["Delete"] == {
        "Objects": [{"Key": "file1.wav"}, {"Key": "file2.wav"}],
        "Quiet": True,
    }

