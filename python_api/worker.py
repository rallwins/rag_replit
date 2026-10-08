import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import SessionFactory, initialize_workspace
from .ingestion import DocumentExtractionError, chunk_pages, extract_pages
from .providers import ProviderMisconfigured, ProviderUnavailable, embed_texts
from .security import Identity, internal_signature
from .storage import ObjectStorageError, delete_private_object, download_private_object

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("rag.worker")
settings = get_settings()
WORKER_ID = f"{os.getenv('HOSTNAME', 'worker')}:{os.getpid()}"
WORK_LEASE_MINUTES = 5
MAX_EMBEDDING_BATCH = 64
_workspace_tasks: dict[str, asyncio.Task[None]] = {}


@asynccontextmanager
async def tenant_transaction(identity: Identity, workspace_id: str) -> AsyncIterator[AsyncSession]:
    if SessionFactory is None:
        raise RuntimeError("Database is not configured")
    async with SessionFactory() as session:
        async with session.begin():
            resolved = await initialize_workspace(session, identity)
            if resolved != workspace_id:
                raise RuntimeError("Worker workspace identity did not match")
            yield session


def _response_signature(
    timestamp: str,
    issuer: str,
    subject: str,
    workspace_id: str,
    signature: str,
) -> bool:
    try:
        issued = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - issued) > 60:
        return False
    expected = internal_signature(timestamp, "POST", "/enqueue", issuer, subject, workspace_id)
    return hmac.compare_digest(signature, expected)


async def _claim_job(identity: Identity, workspace_id: str) -> dict | None:
    async with tenant_transaction(identity, workspace_id) as session:
        result = await session.execute(
            text(
                """
                SELECT j.id AS job_id, j.document_id, j.attempt_count, j.max_attempts,
                   d.collection_id, d.original_filename, d.media_type, d.byte_size, d.sha256,
                       d.storage_key, d.uploaded_by
                FROM ingestion_jobs AS j
                JOIN documents AS d
                  ON d.workspace_id = j.workspace_id AND d.id = j.document_id
                WHERE j.workspace_id = :workspace_id
                  AND (
                    (j.status = 'queued' AND j.available_at <= now())
                    OR (j.status = 'processing' AND (j.lease_until IS NULL OR j.lease_until < now()))
                  )
                  AND d.deleted_at IS NULL
                  AND d.status IN ('queued', 'processing')
                ORDER BY j.available_at, j.created_at
                LIMIT 1
                FOR UPDATE OF j SKIP LOCKED
                """
            ),
            {"workspace_id": workspace_id},
        )
        row = result.mappings().first()
        if not row:
            return None
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET status = 'processing', attempt_count = attempt_count + 1,
                    lease_until = now() + (:lease_minutes * interval '1 minute'),
                    worker_id = :worker_id, updated_at = now()
                WHERE workspace_id = :workspace_id AND id = :job_id
                """
            ),
            {
                "lease_minutes": WORK_LEASE_MINUTES,
                "worker_id": WORKER_ID,
                "workspace_id": workspace_id,
                "job_id": str(row["job_id"]),
            },
        )
        await session.execute(
            text(
                """
                UPDATE documents SET status = 'processing', failure_code = NULL, updated_at = now()
                WHERE workspace_id = :workspace_id AND id = :document_id AND deleted_at IS NULL
                """
            ),
            {"workspace_id": workspace_id, "document_id": str(row["document_id"])},
        )
        return dict(row)


async def _delete_pending(identity: Identity, workspace_id: str) -> int:
    async with tenant_transaction(identity, workspace_id) as session:
        result = await session.execute(
            text(
                """
                SELECT id, storage_key, uploaded_by
                FROM documents
                WHERE workspace_id = :workspace_id AND status = 'deleting'
                ORDER BY updated_at
                LIMIT 25
                """
            ),
            {"workspace_id": workspace_id},
        )
        rows = [dict(row) for row in result.mappings()]
    deleted = 0
    for row in rows:
        try:
            if row["storage_key"]:
                await delete_private_object(str(row["storage_key"]), str(row["uploaded_by"]))
            async with tenant_transaction(identity, workspace_id) as session:
                await session.execute(
                    text(
                        """
                        UPDATE documents
                        SET status = 'deleted', updated_at = now()
                        WHERE workspace_id = :workspace_id AND id = :document_id AND status = 'deleting'
                        """
                    ),
                    {"workspace_id": workspace_id, "document_id": str(row["id"])},
                )
            deleted += 1
        except ObjectStorageError:
            logger.warning(
                "Private object cleanup failed",
                extra={"workspace_id": workspace_id, "document_id": str(row["id"])},
            )
    return deleted


async def _renew_lease(identity: Identity, workspace_id: str, job_id: str) -> None:
    async with tenant_transaction(identity, workspace_id) as session:
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET lease_until = now() + (:lease_minutes * interval '1 minute'), updated_at = now()
                WHERE workspace_id = :workspace_id AND id = :job_id AND status = 'processing'
                """
            ),
            {
                "lease_minutes": WORK_LEASE_MINUTES,
                "workspace_id": workspace_id,
                "job_id": job_id,
            },
        )


async def _persist_chunks(
    identity: Identity,
    workspace_id: str,
    document_id: str,
    collection_id: str,
    chunks: list,
    vectors: list[list[float]],
    model: str,
) -> None:
    if not chunks:
        return
    async with tenant_transaction(identity, workspace_id) as session:
        active = await session.execute(
            text(
                """
                SELECT id FROM documents
                WHERE workspace_id = :workspace_id AND id = :document_id
                  AND status = 'processing' AND deleted_at IS NULL
                FOR UPDATE
                """
            ),
            {"workspace_id": workspace_id, "document_id": document_id},
        )
        if active.scalar_one_or_none() is None:
            raise RuntimeError("Ingestion was cancelled before chunk persistence")
        await session.execute(
            text(
                """
                INSERT INTO document_chunks
                    (workspace_id, collection_id, document_id, ordinal, content, token_count,
                     page_number, start_offset, end_offset, embedding, embedding_model)
                VALUES
                    (:workspace_id, :collection_id, :document_id, :ordinal, :content, :token_count,
                     :page_number, :start_offset, :end_offset, CAST(:embedding AS vector), :embedding_model)
                """
            ),
            [
                {
                    "workspace_id": workspace_id,
                    "collection_id": collection_id,
                    "document_id": document_id,
                    "ordinal": ordinal,
                    "content": chunk.content,
                    "token_count": chunk.token_count,
                    "page_number": chunk.page_number,
                    "start_offset": chunk.start_offset,
                    "end_offset": chunk.end_offset,
                    "embedding": json.dumps(vector),
                    "embedding_model": model,
                }
                for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors))
            ],
        )


async def _process_job(identity: Identity, workspace_id: str, job: dict) -> None:
    job_id = str(job["job_id"])
    document_id = str(job["document_id"])
    try:
        if str(job["uploaded_by"]) != identity.subject:
            raise ObjectStorageError("document_owner_mismatch")
        payload = await download_private_object(
            str(job["storage_key"]),
            str(job["uploaded_by"]),
            settings.upload_max_bytes,
        )
        if len(payload) != int(job["byte_size"]):
            raise ObjectStorageError("uploaded_size_mismatch")
        if hashlib.sha256(payload).hexdigest() != str(job["sha256"]).strip():
            raise ObjectStorageError("uploaded_hash_mismatch")

        pages = extract_pages(payload, str(job["original_filename"]), str(job["media_type"]))
        chunks = chunk_pages(pages)
        if not chunks:
            raise DocumentExtractionError("no_extractable_text")
        async with tenant_transaction(identity, workspace_id) as session:
            await session.execute(
                text(
                    "DELETE FROM document_chunks WHERE workspace_id = :workspace_id AND document_id = :document_id"
                ),
                {"workspace_id": workspace_id, "document_id": document_id},
            )

        for start in range(0, len(chunks), MAX_EMBEDDING_BATCH):
            batch = chunks[start : start + MAX_EMBEDDING_BATCH]
            vectors = await embed_texts([chunk.content for chunk in batch])
            await _persist_chunks(
                identity,
                workspace_id,
                document_id,
                str(job["collection_id"]),
                batch,
                vectors,
                settings.embedding_model,
            )
            await _renew_lease(identity, workspace_id, job_id)

        page_count = len(pages) if all(page.page_number is not None for page in pages) else None
        async with tenant_transaction(identity, workspace_id) as session:
            result = await session.execute(
                text(
                    """
                    UPDATE documents
                    SET status = 'ready', failure_code = NULL, chunk_count = :chunk_count,
                        page_count = :page_count, embedding_provider = :provider,
                        embedding_model = :model, embedding_dimensions = :dimensions, updated_at = now()
                    WHERE workspace_id = :workspace_id AND id = :document_id
                      AND status = 'processing' AND deleted_at IS NULL
                    RETURNING collection_id
                    """
                ),
                {
                    "chunk_count": len(chunks),
                    "page_count": page_count,
                    "provider": settings.embedding_provider,
                    "model": settings.embedding_model,
                    "dimensions": settings.embedding_dimensions,
                    "workspace_id": workspace_id,
                    "document_id": document_id,
                },
            )
            completed = result.mappings().first()
            await session.execute(
                text(
                    """
                    UPDATE ingestion_jobs
                    SET status = 'succeeded', lease_until = NULL, last_error_code = NULL, updated_at = now()
                    WHERE workspace_id = :workspace_id AND id = :job_id
                    """
                ),
                {"workspace_id": workspace_id, "job_id": job_id},
            )
            if completed:
                await session.execute(
                    text(
                        """
                        INSERT INTO activity_events
                            (workspace_id, actor_subject, event_type, resource_type, resource_id, outcome)
                        VALUES
                            (:workspace_id, :actor, 'document.ingested', 'document', :document_id, 'success')
                        """
                    ),
                    {
                        "workspace_id": workspace_id,
                        "actor": identity.subject,
                        "document_id": document_id,
                    },
                )
        logger.info(
            "Document ingestion completed",
            extra={"workspace_id": workspace_id, "document_id": document_id, "chunk_count": len(chunks)},
        )
    except DocumentExtractionError as error:
        await _fail_job(identity, workspace_id, job, error.code)
    except ObjectStorageError as error:
        await _fail_job(identity, workspace_id, job, str(error))
    except ProviderMisconfigured:
        await _fail_job(identity, workspace_id, job, "provider_misconfigured")
    except ProviderUnavailable:
        await _fail_job(identity, workspace_id, job, "provider_unavailable")
    except Exception as error:
        logger.error(
            "Document ingestion failed",
            extra={
                "workspace_id": workspace_id,
                "document_id": document_id,
                "error_type": type(error).__name__,
            },
        )
        await _fail_job(identity, workspace_id, job, "ingestion_internal_error")


async def _fail_job(identity: Identity, workspace_id: str, job: dict, code: str) -> None:
    attempts = int(job["attempt_count"]) + 1
    max_attempts = int(job["max_attempts"])
    terminal = attempts >= max_attempts
    delay_seconds = min(900, 2**min(attempts, 9))
    async with tenant_transaction(identity, workspace_id) as session:
        await session.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET status = :status,
                    available_at = CASE WHEN :terminal THEN available_at
                                        ELSE now() + (:delay * interval '1 second') END,
                    lease_until = NULL, last_error_code = :error_code, updated_at = now()
                WHERE workspace_id = :workspace_id AND id = :job_id
                """
            ),
            {
                "status": "failed" if terminal else "queued",
                "terminal": terminal,
                "delay": delay_seconds,
                "error_code": code[:100],
                "workspace_id": workspace_id,
                "job_id": str(job["job_id"]),
            },
        )
        await session.execute(
            text(
                """
                UPDATE documents
                SET status = :status, failure_code = :failure_code, updated_at = now()
                WHERE workspace_id = :workspace_id AND id = :document_id AND deleted_at IS NULL
                """
            ),
            {
                "status": "failed" if terminal else "queued",
                "failure_code": code[:100] if terminal else None,
                "workspace_id": workspace_id,
                "document_id": str(job["document_id"]),
            },
        )
        if terminal:
            await session.execute(
                text(
                    """
                    INSERT INTO activity_events
                        (workspace_id, actor_subject, event_type, resource_type, resource_id, outcome)
                    VALUES
                        (:workspace_id, :actor, 'document.ingestion_failed', 'document', :document_id, 'failure')
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "actor": identity.subject,
                    "document_id": str(job["document_id"]),
                },
            )
    logger.warning(
        "Document ingestion attempt failed",
        extra={
            "workspace_id": workspace_id,
            "document_id": str(job["document_id"]),
            "error_code": code,
            "terminal": terminal,
        },
    )


async def _run_workspace(identity: Identity, workspace_id: str) -> None:
    try:
        await _delete_pending(identity, workspace_id)
        while True:
            job = await _claim_job(identity, workspace_id)
            if job:
                await _process_job(identity, workspace_id, job)
                continue
            async with tenant_transaction(identity, workspace_id) as session:
                result = await session.execute(
                    text(
                        """
                        SELECT min(available_at) AS next_available
                        FROM ingestion_jobs
                        WHERE workspace_id = :workspace_id AND status = 'queued'
                        """
                    ),
                    {"workspace_id": workspace_id},
                )
                next_available = result.scalar_one_or_none()
            if next_available is None:
                break
            delay = max(0.1, min(30.0, (next_available - datetime.now(timezone.utc)).total_seconds()))
            await asyncio.sleep(delay)
    except Exception as error:
        logger.error(
            "Tenant queue scan failed",
            extra={"workspace_id": workspace_id, "error_type": type(error).__name__},
        )


@asynccontextmanager
async def worker_lifespan(_: FastAPI):
    yield
    for task in list(_workspace_tasks.values()):
        task.cancel()
    if _workspace_tasks:
        await asyncio.gather(*_workspace_tasks.values(), return_exceptions=True)


app = FastAPI(title="RAG Ingestion Worker", docs_url=None, redoc_url=None, lifespan=worker_lifespan)


@app.get("/healthz")
async def health():
    return {"status": "ok"}


@app.post("/enqueue", status_code=202)
async def enqueue_workspace(
    x_rag_issuer: str = Header(default=""),
    x_rag_subject: str = Header(default=""),
    x_rag_workspace: str = Header(default=""),
    x_rag_timestamp: str = Header(default=""),
    x_rag_signature: str = Header(default=""),
):
    if (
        not settings.session_secret
        or not x_rag_issuer
        or not x_rag_subject
        or not x_rag_workspace
        or not _response_signature(
            x_rag_timestamp,
            x_rag_issuer,
            x_rag_subject,
            x_rag_workspace,
            x_rag_signature,
        )
    ):
        raise HTTPException(status_code=403, detail="Invalid internal queue signature")
    try:
        workspace_id = str(UUID(x_rag_workspace))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid workspace") from None

    identity = Identity(issuer=x_rag_issuer, subject=x_rag_subject)
    key = f"{identity.issuer}:{identity.subject}:{workspace_id}"
    existing = _workspace_tasks.get(key)
    if existing is None or existing.done():
        task = asyncio.create_task(_run_workspace(identity, workspace_id))
        _workspace_tasks[key] = task

        def clear_task(finished: asyncio.Task[None]) -> None:
            if _workspace_tasks.get(key) is finished:
                _workspace_tasks.pop(key, None)

        task.add_done_callback(clear_task)
    return {"status": "accepted"}
