import base64
import json
import os
import logging
import re
import time
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import text

from .config import get_settings
from .db import WorkspaceContext, current_workspace
from .providers import (
    ProviderMisconfigured,
    ProviderTimeout,
    ProviderUnavailable,
    embed_texts,
    generate_answer,
)
from .schemas import (
    ActivityEvent,
    ActivityPage,
    AuthSummary,
    Citation,
    CollectionCreate,
    CollectionOut,
    CollectionPage,
    CollectionUpdate,
    Dashboard,
    DocumentOut,
    DocumentPage,
    DocumentUploadInput,
    EmbeddingSummary,
    ProviderSummary,
    QueryRequest,
    QueryResponse,
    RetrievedChunk,
    SearchRequest,
    SearchResponse,
    StorageSummary,
    SystemConfig,
)
from .security import owner_hash
from .storage import (
    ObjectStorageError,
    delete_private_object,
    inspect_uploaded_object,
)

logger = logging.getLogger("rag.api")
router = APIRouter()
DocumentStatus = Literal["queued", "processing", "ready", "failed", "deleting", "deleted"]
ALLOWED_MEDIA_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/html",
    "text/markdown",
    "text/plain",
}


def _cursor_encode(row) -> str:
    value = f"{row['created_at'].isoformat()}|{row['id']}"
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _cursor_decode(value: str | None) -> tuple[datetime, str] | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        created_at, item_id = decoded.split("|", 1)
        return datetime.fromisoformat(created_at), str(UUID(item_id))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_cursor", "title": "The page cursor is invalid"},
        ) from None


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id", "")


async def _activity(
    context: WorkspaceContext,
    request: Request,
    event_type: str,
    resource_type: str | None,
    resource_id: str | None,
    outcome: str = "success",
) -> None:
    await context.session.execute(
        text(
            """
            INSERT INTO activity_events
                (workspace_id, actor_subject, event_type, resource_type, resource_id, request_id, outcome)
            VALUES
                (:workspace_id, :actor, :event_type, :resource_type, :resource_id, :request_id, :outcome)
            """
        ),
        {
            "workspace_id": context.workspace_id,
            "actor": context.identity.subject,
            "event_type": event_type,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "request_id": _request_id(request),
            "outcome": outcome,
        },
    )


def _collection_out(row) -> CollectionOut:
    return CollectionOut(
        id=str(row["id"]),
        name=row["name"],
        description=row["description"] or "",
        document_count=int(row["document_count"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _document_out(row) -> DocumentOut:
    return DocumentOut(
        id=str(row["id"]),
        collection_id=str(row["collection_id"]),
        filename=row["original_filename"],
        media_type=row["media_type"],
        byte_size=int(row["byte_size"]),
        status=row["status"],
        failure_code=row["failure_code"],
        chunk_count=int(row["chunk_count"]),
        page_count=row["page_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _activity_out(row) -> ActivityEvent:
    return ActivityEvent(
        id=str(row["id"]),
        event_type=row["event_type"],
        resource_type=row["resource_type"],
        resource_id=str(row["resource_id"]) if row["resource_id"] else None,
        outcome=row["outcome"],
        created_at=row["created_at"],
    )


async def _get_collection(context: WorkspaceContext, collection_id: UUID):
    result = await context.session.execute(
        text(
            """
            SELECT id, name, description, created_at, updated_at
            FROM collections
            WHERE workspace_id = :workspace_id
              AND id = :collection_id
              AND deleted_at IS NULL
            """
        ),
        {"workspace_id": context.workspace_id, "collection_id": str(collection_id)},
    )
    return result.mappings().first()


@router.get("/dashboard", response_model=Dashboard)
async def get_dashboard(
    context: WorkspaceContext = Depends(current_workspace),
) -> Dashboard:
    counts = await context.session.execute(
        text(
            """
            SELECT
                (SELECT count(*) FROM collections
                  WHERE workspace_id = :workspace_id AND deleted_at IS NULL) AS collection_count,
                (SELECT count(*) FROM documents
                  WHERE workspace_id = :workspace_id AND deleted_at IS NULL) AS document_count,
                (SELECT count(*) FROM documents
                  WHERE workspace_id = :workspace_id AND deleted_at IS NULL AND status = 'ready') AS ready_count,
                (SELECT count(*) FROM documents
                  WHERE workspace_id = :workspace_id AND deleted_at IS NULL
                    AND status IN ('queued', 'processing')) AS processing_count,
                (SELECT count(*) FROM documents
                  WHERE workspace_id = :workspace_id AND deleted_at IS NULL AND status = 'failed') AS failed_count
            """
        ),
        {"workspace_id": context.workspace_id},
    )
    count_row = counts.mappings().one()
    activity = await context.session.execute(
        text(
            """
            SELECT id, event_type, resource_type, resource_id, outcome, created_at
            FROM activity_events
            WHERE workspace_id = :workspace_id
            ORDER BY created_at DESC, id DESC
            LIMIT 8
            """
        ),
        {"workspace_id": context.workspace_id},
    )
    return Dashboard(
        collection_count=int(count_row["collection_count"]),
        document_count=int(count_row["document_count"]),
        ready_document_count=int(count_row["ready_count"]),
        processing_document_count=int(count_row["processing_count"]),
        failed_document_count=int(count_row["failed_count"]),
        recent_activity=[_activity_out(row) for row in activity.mappings()],
    )


@router.get("/activity", response_model=ActivityPage)
async def list_activity(
    context: WorkspaceContext = Depends(current_workspace),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
) -> ActivityPage:
    decoded = _cursor_decode(cursor)
    result = await context.session.execute(
        text(
            """
            SELECT id, event_type, resource_type, resource_id, outcome, created_at
            FROM activity_events
            WHERE workspace_id = :workspace_id
              AND (
                CAST(:cursor_created AS timestamptz) IS NULL
                OR (created_at, id) < (CAST(:cursor_created AS timestamptz), CAST(:cursor_id AS uuid))
              )
            ORDER BY created_at DESC, id DESC
            LIMIT :page_limit
            """
        ),
        {
            "workspace_id": context.workspace_id,
            "cursor_created": decoded[0] if decoded else None,
            "cursor_id": decoded[1] if decoded else None,
            "page_limit": limit + 1,
        },
    )
    rows = list(result.mappings())
    next_cursor = _cursor_encode(rows[limit - 1]) if len(rows) > limit else None
    return ActivityPage(items=[_activity_out(row) for row in rows[:limit]], next_cursor=next_cursor)


@router.get("/system/config", response_model=SystemConfig)
async def system_config(
    context: WorkspaceContext = Depends(current_workspace),
) -> SystemConfig:
    settings = get_settings()
    return SystemConfig(
        llm=ProviderSummary(
            provider=settings.llm_provider,
            model=settings.llm_model,
            configured=settings.llm_configured,
        ),
        embeddings=EmbeddingSummary(
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            configured=settings.embeddings_configured,
        ),
        storage=StorageSummary(
            backend="private-app-storage",
            configured=bool(
                settings.rag_internal_storage_base_url
                and os.getenv("PRIVATE_OBJECT_DIR")
                and os.getenv("DEFAULT_OBJECT_STORAGE_BUCKET_ID")
            ),
        ),
        auth=AuthSummary(
            mode="replit-oidc",
            configured=bool(settings.session_secret and os.getenv("REPL_ID")),
        ),
    )


@router.get("/collections", response_model=CollectionPage)
async def list_collections(
    context: WorkspaceContext = Depends(current_workspace),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
) -> CollectionPage:
    decoded = _cursor_decode(cursor)
    result = await context.session.execute(
        text(
            """
            SELECT c.id, c.name, c.description, c.created_at, c.updated_at,
                   count(d.id) AS document_count
            FROM collections AS c
            LEFT JOIN documents AS d
              ON d.workspace_id = c.workspace_id
             AND d.collection_id = c.id
             AND d.deleted_at IS NULL
            WHERE c.workspace_id = :workspace_id
              AND c.deleted_at IS NULL
              AND (
                CAST(:cursor_created AS timestamptz) IS NULL
                OR (c.created_at, c.id) < (CAST(:cursor_created AS timestamptz), CAST(:cursor_id AS uuid))
              )
            GROUP BY c.id
            ORDER BY c.created_at DESC, c.id DESC
            LIMIT :page_limit
            """
        ),
        {
            "workspace_id": context.workspace_id,
            "cursor_created": decoded[0] if decoded else None,
            "cursor_id": decoded[1] if decoded else None,
            "page_limit": limit + 1,
        },
    )
    rows = list(result.mappings())
    next_cursor = _cursor_encode(rows[limit - 1]) if len(rows) > limit else None
    return CollectionPage(items=[_collection_out(row) for row in rows[:limit]], next_cursor=next_cursor)


@router.post("/collections", response_model=CollectionOut, status_code=201)
async def create_collection(
    body: CollectionCreate,
    request: Request,
    context: WorkspaceContext = Depends(current_workspace),
) -> CollectionOut:
    result = await context.session.execute(
        text(
            """
            INSERT INTO collections (workspace_id, name, description, created_by)
            VALUES (:workspace_id, :name, :description, :created_by)
            RETURNING id, name, description, created_at, updated_at
            """
        ),
        {
            "workspace_id": context.workspace_id,
            "name": body.name,
            "description": body.description,
            "created_by": context.identity.subject,
        },
    )
    row = result.mappings().one()
    row = dict(row)
    row["document_count"] = 0
    await _activity(context, request, "collection.created", "collection", str(row["id"]))
    return _collection_out(row)


@router.get("/collections/{collection_id}", response_model=CollectionOut)
async def get_collection(
    collection_id: UUID,
    context: WorkspaceContext = Depends(current_workspace),
) -> CollectionOut:
    result = await context.session.execute(
        text(
            """
            SELECT c.id, c.name, c.description, c.created_at, c.updated_at,
                   count(d.id) AS document_count
            FROM collections AS c
            LEFT JOIN documents AS d
              ON d.workspace_id = c.workspace_id
             AND d.collection_id = c.id
             AND d.deleted_at IS NULL
            WHERE c.workspace_id = :workspace_id
              AND c.id = :collection_id
              AND c.deleted_at IS NULL
            GROUP BY c.id
            """
        ),
        {"workspace_id": context.workspace_id, "collection_id": str(collection_id)},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "collection_not_found", "title": "Collection not found"})
    return _collection_out(row)


@router.patch("/collections/{collection_id}", response_model=CollectionOut)
async def update_collection(
    collection_id: UUID,
    body: CollectionUpdate,
    request: Request,
    context: WorkspaceContext = Depends(current_workspace),
) -> CollectionOut:
    changes = body.model_dump(exclude_unset=True)
    if not changes or any(value is None for value in changes.values()):
        raise HTTPException(status_code=422, detail={"code": "empty_update", "title": "Provide at least one collection field"})
    sets = []
    params: dict[str, object] = {
        "workspace_id": context.workspace_id,
        "collection_id": str(collection_id),
    }
    for key, value in changes.items():
        sets.append(f"{key} = :{key}")
        params[key] = value
    sets.append("updated_at = now()")
    result = await context.session.execute(
        text(
            f"""
            UPDATE collections SET {", ".join(sets)}
            WHERE workspace_id = :workspace_id
              AND id = :collection_id
              AND deleted_at IS NULL
            RETURNING id, name, description, created_at, updated_at
            """
        ),
        params,
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "collection_not_found", "title": "Collection not found"})
    row = dict(row)
    count = await context.session.execute(
        text(
            """
            SELECT count(*) FROM documents
            WHERE workspace_id = :workspace_id AND collection_id = :collection_id AND deleted_at IS NULL
            """
        ),
        {"workspace_id": context.workspace_id, "collection_id": str(collection_id)},
    )
    row["document_count"] = int(count.scalar_one())
    await _activity(context, request, "collection.updated", "collection", str(collection_id))
    return _collection_out(row)


@router.delete("/collections/{collection_id}", status_code=204)
async def delete_collection(
    collection_id: UUID,
    request: Request,
    context: WorkspaceContext = Depends(current_workspace),
) -> Response:
    result = await context.session.execute(
        text(
            """
            UPDATE collections
            SET deleted_at = now(), updated_at = now()
            WHERE workspace_id = :workspace_id AND id = :collection_id AND deleted_at IS NULL
            RETURNING id
            """
        ),
        {"workspace_id": context.workspace_id, "collection_id": str(collection_id)},
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail={"code": "collection_not_found", "title": "Collection not found"})
    await context.session.execute(
        text(
            """
            UPDATE documents
            SET status = 'deleting', deleted_at = now(), updated_at = now()
            WHERE workspace_id = :workspace_id AND collection_id = :collection_id
              AND status <> 'deleted'
            """
        ),
        {"workspace_id": context.workspace_id, "collection_id": str(collection_id)},
    )
    await context.session.execute(
        text("DELETE FROM document_chunks WHERE workspace_id = :workspace_id AND collection_id = :collection_id"),
        {"workspace_id": context.workspace_id, "collection_id": str(collection_id)},
    )
    await context.session.execute(
        text(
            """
            UPDATE ingestion_jobs SET status = 'cancelled', lease_until = NULL, updated_at = now()
            WHERE workspace_id = :workspace_id
              AND document_id IN (SELECT id FROM documents WHERE workspace_id = :workspace_id AND collection_id = :collection_id)
              AND status IN ('queued', 'processing')
            """
        ),
        {"workspace_id": context.workspace_id, "collection_id": str(collection_id)},
    )
    await _activity(context, request, "collection.deleted", "collection", str(collection_id))
    return Response(status_code=204)


@router.get("/collections/{collection_id}/documents", response_model=DocumentPage)
async def list_documents(
    collection_id: UUID,
    context: WorkspaceContext = Depends(current_workspace),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
    status: DocumentStatus | None = None,
) -> DocumentPage:
    await _require_collection(context, collection_id)
    decoded = _cursor_decode(cursor)
    result = await context.session.execute(
        text(
            """
            SELECT id, collection_id, original_filename, media_type, byte_size, status,
                   failure_code, chunk_count, page_count, created_at, updated_at
            FROM documents
            WHERE workspace_id = :workspace_id AND collection_id = :collection_id
              AND deleted_at IS NULL
              AND (CAST(:status AS text) IS NULL OR status = CAST(:status AS text))
              AND (
                CAST(:cursor_created AS timestamptz) IS NULL
                OR (created_at, id) < (CAST(:cursor_created AS timestamptz), CAST(:cursor_id AS uuid))
              )
            ORDER BY created_at DESC, id DESC
            LIMIT :page_limit
            """
        ),
        {
            "workspace_id": context.workspace_id,
            "collection_id": str(collection_id),
            "status": status,
            "cursor_created": decoded[0] if decoded else None,
            "cursor_id": decoded[1] if decoded else None,
            "page_limit": limit + 1,
        },
    )
    rows = list(result.mappings())
    next_cursor = _cursor_encode(rows[limit - 1]) if len(rows) > limit else None
    return DocumentPage(items=[_document_out(row) for row in rows[:limit]], next_cursor=next_cursor)


async def _require_collection(context: WorkspaceContext, collection_id: UUID) -> None:
    if not await _get_collection(context, collection_id):
        raise HTTPException(status_code=404, detail={"code": "collection_not_found", "title": "Collection not found"})


def _clean_filename(value: str) -> str:
    filename = value.replace("\\", "/").split("/")[-1].strip()
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)
    return filename[:255] or "untitled-document"


async def _remove_upload(object_path: str, subject: str) -> None:
    try:
        await delete_private_object(object_path, subject)
    except ObjectStorageError:
        logger.warning("orphaned upload cleanup could not be completed")


@router.post(
    "/collections/{collection_id}/documents",
    response_model=DocumentOut,
    status_code=202,
)
async def finalize_document_upload(
    collection_id: UUID,
    body: DocumentUploadInput,
    request: Request,
    context: WorkspaceContext = Depends(current_workspace),
) -> DocumentOut:
    await _require_collection(context, collection_id)
    settings = get_settings()
    media_type = body.contentType.lower().split(";")[0].strip()
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=415, detail={"code": "unsupported_media_type", "title": "Document type is not supported"})
    if body.size > settings.upload_max_bytes:
        raise HTTPException(status_code=413, detail={"code": "upload_too_large", "title": "Document exceeds the upload limit"})
    if not body.objectPath.startswith(f"/objects/uploads/{owner_hash(context.identity.subject)}/"):
        raise HTTPException(status_code=403, detail={"code": "storage_path_not_owned", "title": "Uploaded file is not owned by this user"})
    try:
        sha256, actual_size, actual_media_type = await inspect_uploaded_object(
            body.objectPath,
            context.identity.subject,
            body.size,
            settings.upload_max_bytes,
        )
    except ObjectStorageError as error:
        code = str(error)
        status_code = 404 if code == "uploaded_object_not_found" else 413 if code == "upload_too_large" else 422 if code.endswith("mismatch") else 503
        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "title": "Uploaded file could not be verified"},
        ) from None
    if actual_media_type not in ALLOWED_MEDIA_TYPES or actual_media_type != media_type:
        await _remove_upload(body.objectPath, context.identity.subject)
        raise HTTPException(status_code=415, detail={"code": "uploaded_media_type_mismatch", "title": "Uploaded file type does not match its metadata"})
    if actual_size != body.size:
        await _remove_upload(body.objectPath, context.identity.subject)
        raise HTTPException(status_code=422, detail={"code": "uploaded_size_mismatch", "title": "Uploaded file size does not match its metadata"})

    existing_result = await context.session.execute(
        text(
            """
            SELECT id, collection_id, original_filename, media_type, byte_size, status,
                   failure_code, chunk_count, page_count, created_at, updated_at, deleted_at
            FROM documents
            WHERE workspace_id = :workspace_id AND collection_id = :collection_id AND sha256 = :sha256
            """
        ),
        {
            "workspace_id": context.workspace_id,
            "collection_id": str(collection_id),
            "sha256": sha256,
        },
    )
    existing = existing_result.mappings().first()
    if existing and existing["deleted_at"] is None:
        await _remove_upload(body.objectPath, context.identity.subject)
        return _document_out(existing)
    if not settings.embeddings_configured:
        await _remove_upload(body.objectPath, context.identity.subject)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "provider_misconfigured",
                "title": "Document indexing is unavailable until an embeddings provider is configured",
            },
        )
    if existing:
        document_id = str(existing["id"])
        updated = await context.session.execute(
            text(
                """
                UPDATE documents
                SET original_filename = :filename, media_type = :media_type, byte_size = :byte_size,
                    storage_key = :storage_key, status = 'queued', failure_code = NULL,
                    chunk_count = 0, page_count = NULL, uploaded_by = :uploaded_by,
                    deleted_at = NULL, updated_at = now()
                WHERE workspace_id = :workspace_id AND id = :document_id
                RETURNING id, collection_id, original_filename, media_type, byte_size, status,
                          failure_code, chunk_count, page_count, created_at, updated_at
                """
            ),
            {
                "filename": _clean_filename(body.name),
                "media_type": media_type,
                "byte_size": actual_size,
                "storage_key": body.objectPath,
                "uploaded_by": context.identity.subject,
                "workspace_id": context.workspace_id,
                "document_id": document_id,
            },
        )
        row = updated.mappings().one()
        await context.session.execute(
            text(
                """
                INSERT INTO ingestion_jobs (workspace_id, document_id, status)
                VALUES (:workspace_id, :document_id, 'queued')
                """
            ),
            {"workspace_id": context.workspace_id, "document_id": document_id},
        )
    else:
        inserted = await context.session.execute(
            text(
                """
                INSERT INTO documents
                    (workspace_id, collection_id, original_filename, media_type, byte_size,
                     sha256, storage_key, status, uploaded_by)
                VALUES
                    (:workspace_id, :collection_id, :filename, :media_type, :byte_size,
                     :sha256, :storage_key, 'queued', :uploaded_by)
                ON CONFLICT (workspace_id, collection_id, sha256) DO NOTHING
                RETURNING id, collection_id, original_filename, media_type, byte_size, status,
                          failure_code, chunk_count, page_count, created_at, updated_at
                """
            ),
            {
                "workspace_id": context.workspace_id,
                "collection_id": str(collection_id),
                "filename": _clean_filename(body.name),
                "media_type": media_type,
                "byte_size": actual_size,
                "sha256": sha256,
                "storage_key": body.objectPath,
                "uploaded_by": context.identity.subject,
            },
        )
        row = inserted.mappings().first()
        if row:
            await context.session.execute(
                text(
                    """
                    INSERT INTO ingestion_jobs (workspace_id, document_id, status)
                    VALUES (:workspace_id, :document_id, 'queued')
                    """
                ),
                {"workspace_id": context.workspace_id, "document_id": str(row["id"])},
            )
        else:
            duplicate_result = await context.session.execute(
                text(
                    """
                    SELECT id, collection_id, original_filename, media_type, byte_size, status,
                           failure_code, chunk_count, page_count, created_at, updated_at
                    FROM documents
                    WHERE workspace_id = :workspace_id AND collection_id = :collection_id AND sha256 = :sha256
                      AND deleted_at IS NULL
                    """
                ),
                {
                    "workspace_id": context.workspace_id,
                    "collection_id": str(collection_id),
                    "sha256": sha256,
                },
            )
            row = duplicate_result.mappings().first()
            if not row:
                raise HTTPException(status_code=409, detail={"code": "duplicate_upload_conflict", "title": "Document upload conflicts with an existing record"})
            await _remove_upload(body.objectPath, context.identity.subject)
    await _activity(context, request, "document.queued", "document", str(row["id"]))
    return _document_out(row)


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: UUID,
    context: WorkspaceContext = Depends(current_workspace),
) -> DocumentOut:
    result = await context.session.execute(
        text(
            """
            SELECT id, collection_id, original_filename, media_type, byte_size, status,
                   failure_code, chunk_count, page_count, created_at, updated_at
            FROM documents
            WHERE workspace_id = :workspace_id AND id = :document_id
            """
        ),
        {"workspace_id": context.workspace_id, "document_id": str(document_id)},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail={"code": "document_not_found", "title": "Document not found"})
    return _document_out(row)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: UUID,
    request: Request,
    context: WorkspaceContext = Depends(current_workspace),
) -> Response:
    result = await context.session.execute(
        text(
            """
            UPDATE documents
            SET status = 'deleting', deleted_at = now(), updated_at = now()
            WHERE workspace_id = :workspace_id AND id = :document_id AND status <> 'deleted'
            RETURNING id
            """
        ),
        {"workspace_id": context.workspace_id, "document_id": str(document_id)},
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail={"code": "document_not_found", "title": "Document not found"})
    await context.session.execute(
        text("DELETE FROM document_chunks WHERE workspace_id = :workspace_id AND document_id = :document_id"),
        {"workspace_id": context.workspace_id, "document_id": str(document_id)},
    )
    await context.session.execute(
        text(
            """
            UPDATE ingestion_jobs SET status = 'cancelled', lease_until = NULL, updated_at = now()
            WHERE workspace_id = :workspace_id AND document_id = :document_id
              AND status IN ('queued', 'processing')
            """
        ),
        {"workspace_id": context.workspace_id, "document_id": str(document_id)},
    )
    await _activity(context, request, "document.deleted", "document", str(document_id))
    return Response(status_code=204)


@router.post("/documents/{document_id}/retry", response_model=DocumentOut, status_code=202)
async def retry_document(
    document_id: UUID,
    request: Request,
    context: WorkspaceContext = Depends(current_workspace),
) -> DocumentOut:
    result = await context.session.execute(
        text(
            """
            UPDATE documents
            SET status = 'queued', failure_code = NULL, chunk_count = 0, page_count = NULL, updated_at = now()
            WHERE workspace_id = :workspace_id AND id = :document_id AND status = 'failed'
              AND deleted_at IS NULL
            RETURNING id, collection_id, original_filename, media_type, byte_size, status,
                      failure_code, chunk_count, page_count, created_at, updated_at
            """
        ),
        {"workspace_id": context.workspace_id, "document_id": str(document_id)},
    )
    row = result.mappings().first()
    if not row:
        exists = await context.session.execute(
            text("SELECT 1 FROM documents WHERE workspace_id = :workspace_id AND id = :document_id"),
            {"workspace_id": context.workspace_id, "document_id": str(document_id)},
        )
        if exists.scalar_one_or_none():
            raise HTTPException(status_code=409, detail={"code": "document_not_retryable", "title": "Only failed documents can be retried"})
        raise HTTPException(status_code=404, detail={"code": "document_not_found", "title": "Document not found"})
    await context.session.execute(
        text("DELETE FROM document_chunks WHERE workspace_id = :workspace_id AND document_id = :document_id"),
        {"workspace_id": context.workspace_id, "document_id": str(document_id)},
    )
    await context.session.execute(
        text(
            """
            INSERT INTO ingestion_jobs (workspace_id, document_id, status)
            VALUES (:workspace_id, :document_id, 'queued')
            """
        ),
        {"workspace_id": context.workspace_id, "document_id": str(document_id)},
    )
    await _activity(context, request, "document.retry_queued", "document", str(document_id))
    return _document_out(row)


async def _retrieve(
    context: WorkspaceContext,
    collection_id: UUID,
    query: str,
    top_k: int,
    min_score: float | None,
) -> list[dict]:
    await _require_collection(context, collection_id)
    settings = get_settings()
    embedding = (await embed_texts([query]))[0]
    threshold = settings.retrieval_min_score if min_score is None else min_score
    result = await context.session.execute(
        text(
            """
            SELECT dc.id AS chunk_id, dc.document_id, d.original_filename AS document_name,
                   dc.content, dc.page_number, dc.start_offset, dc.end_offset,
                   1.0 - (dc.embedding <=> CAST(:embedding AS vector)) AS score
            FROM document_chunks AS dc
            JOIN documents AS d
              ON d.workspace_id = dc.workspace_id AND d.id = dc.document_id
            JOIN collections AS c
              ON c.workspace_id = dc.workspace_id AND c.id = dc.collection_id
            WHERE dc.workspace_id = :workspace_id
              AND dc.collection_id = :collection_id
              AND d.status = 'ready'
              AND d.deleted_at IS NULL
              AND c.deleted_at IS NULL
              AND 1.0 - (dc.embedding <=> CAST(:embedding AS vector)) >= :min_score
            ORDER BY dc.embedding <=> CAST(:embedding AS vector) ASC
            LIMIT :top_k
            """
        ),
        {
            "embedding": json.dumps(embedding),
            "workspace_id": context.workspace_id,
            "collection_id": str(collection_id),
            "min_score": threshold,
            "top_k": top_k,
        },
    )
    sources = []
    for row in result.mappings():
        raw_score = float(row["score"])
        score = max(0.0, min(1.0, raw_score))
        sources.append(
            {
                "chunk_id": str(row["chunk_id"]),
                "document_id": str(row["document_id"]),
                "document_name": row["document_name"],
                "content": row["content"],
                "page_number": row["page_number"],
                "start_offset": row["start_offset"],
                "end_offset": row["end_offset"],
                "score": score,
            }
        )
    return sources


async def _record_metrics(
    context: WorkspaceContext,
    request: Request,
    collection_id: UUID,
    *,
    retrieved_count: int,
    retrieval_ms: int,
    generation_ms: int | None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    outcome: Literal["success", "no_sources", "failure"],
) -> None:
    settings = get_settings()
    await context.session.execute(
        text(
            """
            INSERT INTO query_metrics
                (workspace_id, collection_id, actor_subject, request_id,
                 llm_provider, llm_model, embedding_provider, embedding_model,
                 retrieved_count, prompt_tokens, completion_tokens,
                 retrieval_ms, generation_ms, outcome)
            VALUES
                (:workspace_id, :collection_id, :actor_subject, :request_id,
                 :llm_provider, :llm_model, :embedding_provider, :embedding_model,
                 :retrieved_count, :prompt_tokens, :completion_tokens,
                 :retrieval_ms, :generation_ms, :outcome)
            """
        ),
        {
            "workspace_id": context.workspace_id,
            "collection_id": str(collection_id),
            "actor_subject": context.identity.subject,
            "request_id": _request_id(request) or str(uuid4()),
            "llm_provider": settings.llm_provider if settings.llm_configured else None,
            "llm_model": settings.llm_model if settings.llm_configured else None,
            "embedding_provider": settings.embedding_provider if settings.embeddings_configured else None,
            "embedding_model": settings.embedding_model if settings.embeddings_configured else None,
            "retrieved_count": retrieved_count,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "retrieval_ms": max(0, retrieval_ms),
            "generation_ms": max(0, generation_ms) if generation_ms is not None else None,
            "outcome": outcome,
        },
    )


def _raise_provider_error(error: ProviderMisconfigured | ProviderUnavailable) -> None:
    if isinstance(error, ProviderMisconfigured):
        raise HTTPException(
            status_code=503,
            detail={"code": "provider_misconfigured", "title": "An AI provider is not configured"},
        ) from None
    status_code = 504 if isinstance(error, ProviderTimeout) else 503
    code = "provider_timeout" if status_code == 504 else "provider_unavailable"
    title = "The AI provider timed out" if status_code == 504 else "An AI provider is temporarily unavailable"
    raise HTTPException(status_code=status_code, detail={"code": code, "title": title}) from None


@router.post("/collections/{collection_id}/search", response_model=SearchResponse)
async def search_collection(
    collection_id: UUID,
    body: SearchRequest,
    request: Request,
    context: WorkspaceContext = Depends(current_workspace),
) -> SearchResponse:
    started = time.perf_counter()
    try:
        sources = await _retrieve(context, collection_id, body.query, body.top_k, body.min_score)
    except (ProviderMisconfigured, ProviderUnavailable) as error:
        retrieval_ms = int((time.perf_counter() - started) * 1000)
        await _record_metrics(
            context,
            request,
            collection_id,
            retrieved_count=0,
            retrieval_ms=retrieval_ms,
            generation_ms=None,
            outcome="failure",
        )
        _raise_provider_error(error)
    retrieval_ms = int((time.perf_counter() - started) * 1000)
    await _record_metrics(
        context,
        request,
        collection_id,
        retrieved_count=len(sources),
        retrieval_ms=retrieval_ms,
        generation_ms=None,
        outcome="success",
    )
    return SearchResponse(
        query=body.query,
        results=[RetrievedChunk(**source) for source in sources],
    )


@router.post("/collections/{collection_id}/query", response_model=QueryResponse)
async def query_collection(
    collection_id: UUID,
    body: QueryRequest,
    request: Request,
    context: WorkspaceContext = Depends(current_workspace),
) -> QueryResponse:
    started = time.perf_counter()
    query_id = str(uuid4())
    retrieval_ms = 0
    try:
        retrieval_started = time.perf_counter()
        sources = await _retrieve(context, collection_id, body.question, body.top_k, body.min_score)
        retrieval_ms = int((time.perf_counter() - retrieval_started) * 1000)
        if not sources:
            await _record_metrics(
                context,
                request,
                collection_id,
                retrieved_count=0,
                retrieval_ms=retrieval_ms,
                generation_ms=0,
                outcome="no_sources",
            )
            return QueryResponse(
                query_id=query_id,
                answer="No relevant passages were found in this collection, so I cannot answer from its documents.",
                citations=[],
                retrieved_count=0,
                outcome="no_sources",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        generation_started = time.perf_counter()
        completion = await generate_answer(body.question, sources)
        generation_ms = int((time.perf_counter() - generation_started) * 1000)
    except (ProviderMisconfigured, ProviderUnavailable) as error:
        await _record_metrics(
            context,
            request,
            collection_id,
            retrieved_count=len(locals().get("sources", [])),
            retrieval_ms=retrieval_ms,
            generation_ms=None,
            outcome="failure",
        )
        _raise_provider_error(error)

    citations = [
        Citation(
            citation_id=str(index),
            chunk_id=source["chunk_id"],
            document_id=source["document_id"],
            document_name=source["document_name"],
            excerpt=source["content"][:500],
            score=source["score"],
            page_number=source["page_number"],
            start_offset=source["start_offset"],
            end_offset=source["end_offset"],
        )
        for index, source in enumerate(sources, start=1)
    ]
    await _record_metrics(
        context,
        request,
        collection_id,
        retrieved_count=len(sources),
        retrieval_ms=retrieval_ms,
        generation_ms=generation_ms,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        outcome="success",
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "Grounded query completed",
        extra={
            "request_id": _request_id(request),
            "retrieved_count": len(sources),
            "latency_ms": latency_ms,
            "outcome": "success",
        },
    )
    return QueryResponse(
        query_id=query_id,
        answer=completion.content,
        citations=citations,
        retrieved_count=len(sources),
        outcome="answered",
        latency_ms=latency_ms,
    )
