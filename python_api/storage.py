import hashlib
import time

import httpx

from .config import get_settings
from .security import internal_signature, owner_hash


class ObjectStorageError(Exception):
    pass


def _object_route(object_path: str, subject: str) -> tuple[str, str]:
    if not object_path.startswith("/objects/uploads/"):
        raise ObjectStorageError("storage_path_invalid")
    relative_path = object_path.removeprefix("/objects/")
    segments = relative_path.split("/")
    if (
        len(segments) != 3
        or segments[0] != "uploads"
        or segments[1] != owner_hash(subject)
        or len(segments[2]) != 36
    ):
        raise ObjectStorageError("storage_path_not_owned")
    base_url = get_settings().rag_internal_storage_base_url.rstrip("/")
    if not base_url:
        raise ObjectStorageError("storage_not_configured")
    return relative_path, f"{base_url}/objects/{relative_path}"


def _headers(method: str, relative_path: str, subject: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    route_path = f"/api/internal/storage/objects/{relative_path}"
    owner = owner_hash(subject)
    signature = internal_signature(timestamp, method, route_path, owner, subject)
    return {
        "x-rag-subject": subject,
        "x-rag-timestamp": timestamp,
        "x-rag-signature": signature,
    }


async def inspect_uploaded_object(
    object_path: str,
    subject: str,
    expected_size: int,
    max_bytes: int,
) -> tuple[str, int, str]:
    relative_path, url = _object_route(object_path, subject)
    digest = hashlib.sha256()
    actual_size = 0
    timeout = httpx.Timeout(90.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", url, headers=_headers("GET", relative_path, subject)) as response:
                if response.status_code == 404:
                    raise ObjectStorageError("uploaded_object_not_found")
                if response.status_code != 200:
                    raise ObjectStorageError("storage_unavailable")
                content_type = response.headers.get("content-type", "application/octet-stream")
                async for part in response.aiter_bytes(256 * 1024):
                    actual_size += len(part)
                    if actual_size > max_bytes:
                        raise ObjectStorageError("upload_too_large")
                    digest.update(part)
    except httpx.HTTPError as error:
        raise ObjectStorageError("storage_unavailable") from error
    if actual_size != expected_size:
        raise ObjectStorageError("uploaded_size_mismatch")
    return digest.hexdigest(), actual_size, content_type.split(";")[0].strip().lower()


async def download_private_object(object_path: str, subject: str, max_bytes: int) -> bytes:
    relative_path, url = _object_route(object_path, subject)
    timeout = httpx.Timeout(90.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=_headers("GET", relative_path, subject))
        if response.status_code != 200:
            raise ObjectStorageError("storage_unavailable")
        if len(response.content) > max_bytes:
            raise ObjectStorageError("upload_too_large")
        return response.content
    except httpx.HTTPError as error:
        raise ObjectStorageError("storage_unavailable") from error


async def delete_private_object(object_path: str, subject: str) -> None:
    relative_path, url = _object_route(object_path, subject)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            response = await client.delete(
                url,
                headers=_headers("DELETE", relative_path, subject),
            )
        if response.status_code not in {204, 404}:
            raise ObjectStorageError("storage_unavailable")
    except httpx.HTTPError as error:
        raise ObjectStorageError("storage_unavailable") from error
