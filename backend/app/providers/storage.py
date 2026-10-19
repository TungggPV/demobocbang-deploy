"""Private object-storage boundary for durable media and exports."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings


class StorageError(Exception):
    """A storage operation failed without exposing provider credentials."""


class StorageNotFoundError(StorageError):
    """The requested object does not exist."""


class StorageBackend(Protocol):
    async def put_file(self, source: Path, key: str) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def put_bytes(self, content: bytes, key: str) -> None: ...
    async def get_bytes(self, key: str) -> bytes: ...
    async def get_range(self, key: str, start: int, end: int) -> bytes: ...
    async def download_file(self, key: str, destination: Path) -> None: ...
    async def presigned_get_url(self, key: str, expires_in: int, *, content_type: str | None = None, content_disposition: str | None = None) -> str | None: ...


# Compatibility alias for existing service imports.
MediaStorage = StorageBackend


class LocalStorageBackend:
    """Stores opaque object keys below one configured local development root."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    async def put_file(self, source: Path, key: str) -> None:
        destination = self._path_for(key)
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, source, destination)

    async def delete(self, key: str) -> None:
        path = self._path_for(key)
        if path.exists():
            await asyncio.to_thread(path.unlink)

    async def put_bytes(self, content: bytes, key: str) -> None:
        destination = self._path_for(key)
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(destination.write_bytes, content)

    async def get_bytes(self, key: str) -> bytes:
        try:
            return await asyncio.to_thread(self._path_for(key).read_bytes)
        except FileNotFoundError as exc:
            raise StorageNotFoundError("Object was not found.") from exc

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        path = self._path_for(key)
        def read() -> bytes:
            with path.open("rb") as source:
                source.seek(start)
                return source.read(end - start + 1)
        try:
            return await asyncio.to_thread(read)
        except FileNotFoundError as exc:
            raise StorageNotFoundError("Object was not found.") from exc

    async def download_file(self, key: str, destination: Path) -> None:
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(shutil.copyfile, self._path_for(key), destination)
        except FileNotFoundError as exc:
            raise StorageNotFoundError("Object was not found.") from exc

    async def presigned_get_url(self, key: str, expires_in: int, **_: object) -> str | None:
        return None

    def _path_for(self, key: str) -> Path:
        candidate = (self._root / key).resolve()
        if self._root != candidate and self._root not in candidate.parents:
            raise StorageError("Storage key resolves outside the configured media root.")
        return candidate


class S3StorageBackend:
    """Synchronous boto3 client isolated behind async application operations."""

    def __init__(self, *, endpoint_url: str | None, access_key_id: str, secret_access_key: str, bucket_name: str, region: str) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("boto3 is required when STORAGE_BACKEND=s3.") from exc
        self._bucket_name = bucket_name
        self._client = boto3.client("s3", endpoint_url=endpoint_url, aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key, region_name=region)

    async def put_file(self, source: Path, key: str) -> None:
        await self._call(self._client.upload_file, str(source), self._bucket_name, key)

    async def delete(self, key: str) -> None:
        await self._call(self._client.delete_object, Bucket=self._bucket_name, Key=key)

    async def put_bytes(self, content: bytes, key: str) -> None:
        await self._call(self._client.put_object, Bucket=self._bucket_name, Key=key, Body=content)

    async def get_bytes(self, key: str) -> bytes:
        response = await self._call(self._client.get_object, Bucket=self._bucket_name, Key=key)
        return await asyncio.to_thread(response["Body"].read)

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        response = await self._call(self._client.get_object, Bucket=self._bucket_name, Key=key, Range=f"bytes={start}-{end}")
        return await asyncio.to_thread(response["Body"].read)

    async def download_file(self, key: str, destination: Path) -> None:
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await self._call(self._client.download_file, self._bucket_name, key, str(destination))

    async def presigned_get_url(self, key: str, expires_in: int, *, content_type: str | None = None, content_disposition: str | None = None) -> str:
        parameters: dict[str, str] = {"Bucket": self._bucket_name, "Key": key}
        if content_type:
            parameters["ResponseContentType"] = content_type
        if content_disposition:
            parameters["ResponseContentDisposition"] = content_disposition
        return await self._call(self._client.generate_presigned_url, "get_object", Params=parameters, ExpiresIn=expires_in)

    async def _call(self, function: object, /, *args: object, **kwargs: object):
        try:
            return await asyncio.to_thread(function, *args, **kwargs)  # type: ignore[arg-type]
        except Exception as exc:
            code = _client_error_code(exc)
            if code in {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}:
                raise StorageNotFoundError("Object was not found.") from exc
            raise StorageError("S3-compatible storage operation failed.") from exc


class DatabaseStorageBackend:
    """Stores binary media objects in PostgreSQL for durable persistence across container restarts."""

    def __init__(self, database_url: str, local_root: Path | None = None) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._local_root: Path | None = local_root.resolve() if local_root else None

    async def put_file(self, source: Path, key: str) -> None:
        content = await asyncio.to_thread(source.read_bytes)
        await self.put_bytes(content, key)

    async def put_bytes(self, content: bytes, key: str) -> None:
        query = text(
            """
            INSERT INTO storage_blobs (storage_key, data, size_bytes)
            VALUES (:key, :data, :size)
            ON CONFLICT (storage_key) DO UPDATE
            SET data = EXCLUDED.data,
                size_bytes = EXCLUDED.size_bytes,
                updated_at = NOW()
            """
        )
        try:
            async with self._engine.begin() as conn:
                await conn.execute(query, {"key": key, "data": content, "size": len(content)})
        except Exception as exc:
            raise StorageError("Database storage put operation failed.") from exc

        if self._local_root:
            dest = self._local_root / key
            try:
                await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(dest.write_bytes, content)
            except OSError:
                pass

    async def get_bytes(self, key: str) -> bytes:
        if self._local_root:
            local_path = self._local_root / key
            if local_path.is_file():
                try:
                    return await asyncio.to_thread(local_path.read_bytes)
                except OSError:
                    pass
        query = text("SELECT data FROM storage_blobs WHERE storage_key = :key")
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(query, {"key": key})
                data = result.scalar()
                if data is None:
                    raise StorageNotFoundError("Object was not found.")
                return bytes(data)
        except StorageNotFoundError:
            raise
        except Exception as exc:
            raise StorageError("Database storage read operation failed.") from exc

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        if self._local_root:
            local_path = self._local_root / key
            if local_path.is_file():
                try:
                    def read() -> bytes:
                        with local_path.open("rb") as s:
                            s.seek(start)
                            return s.read(end - start + 1)
                    return await asyncio.to_thread(read)
                except OSError:
                    pass
        length = max(0, end - start + 1)
        query = text(
            "SELECT substring(data FROM :start_pos FOR :length) FROM storage_blobs WHERE storage_key = :key"
        )
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(
                    query, {"key": key, "start_pos": start + 1, "length": length}
                )
                data = result.scalar()
                if data is None:
                    raise StorageNotFoundError("Object was not found.")
                return bytes(data)
        except StorageNotFoundError:
            raise
        except Exception as exc:
            raise StorageError("Database storage range operation failed.") from exc

    async def download_file(self, key: str, destination: Path) -> None:
        data = await self.get_bytes(key)
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(destination.write_bytes, data)

    async def delete(self, key: str) -> None:
        query = text("DELETE FROM storage_blobs WHERE storage_key = :key")
        try:
            async with self._engine.begin() as conn:
                await conn.execute(query, {"key": key})
        except Exception as exc:
            raise StorageError("Database storage delete operation failed.") from exc

        if self._local_root:
            local_path = self._local_root / key
            if local_path.exists():
                try:
                    await asyncio.to_thread(local_path.unlink)
                except OSError:
                    pass

    async def presigned_get_url(self, key: str, expires_in: int, **_: object) -> str | None:
        return None

    async def dispose(self) -> None:
        await self._engine.dispose()


def build_storage_backend(settings: Settings) -> StorageBackend:
    if settings.storage_backend in {"s3", "s3-compatible"}:
        missing = [
            name
            for name, value in {
                "S3_ACCESS_KEY_ID": settings.s3_access_key_id,
                "S3_SECRET_ACCESS_KEY": settings.s3_secret_access_key,
                "S3_BUCKET_NAME": settings.s3_bucket_name,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError("STORAGE_BACKEND=s3 requires " + ", ".join(missing) + ".")
        return S3StorageBackend(
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            bucket_name=settings.s3_bucket_name,
            region=settings.s3_region,
        )
    if settings.database_url and (
        settings.storage_backend in {"database", "postgres", "db"}
        or settings.environment.lower() in {"production", "prod"}
    ):
        return DatabaseStorageBackend(settings.database_url, settings.media_storage_path)
    return LocalStorageBackend(settings.media_storage_path)


def _client_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, dict) else None
    return str(details.get("Code")) if isinstance(details, dict) and details.get("Code") else None


FileSystemMediaStorage = LocalStorageBackend
