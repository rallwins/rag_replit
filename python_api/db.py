import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings
from .security import Identity

settings = get_settings()


def _async_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


if settings.database_url:
    engine = create_async_engine(
        _async_database_url(settings.database_url),
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=5,
        pool_recycle=1800,
    )
    SessionFactory = async_sessionmaker(engine, expire_on_commit=False)
else:
    engine = None
    SessionFactory = None


@dataclass
class WorkspaceContext:
    session: AsyncSession
    identity: Identity
    workspace_id: str


async def initialize_workspace(session: AsyncSession, identity: Identity) -> str:
    external_id = f"{identity.issuer}:{identity.subject}"
    await session.execute(
        text("SELECT set_config('app.issuer', :issuer, true), set_config('app.subject', :subject, true)"),
        {"issuer": identity.issuer, "subject": identity.subject},
    )
    inserted = await session.execute(
        text(
            """
            INSERT INTO workspaces (external_id, name)
            VALUES (:external_id, 'Personal workspace')
            ON CONFLICT (external_id) DO NOTHING
            RETURNING id
            """
        ),
        {"external_id": external_id},
    )
    workspace_id = inserted.scalar_one_or_none()
    if workspace_id is None:
        result = await session.execute(
            text("SELECT id FROM workspaces WHERE external_id = :external_id"),
            {"external_id": external_id},
        )
        workspace_id = result.scalar_one_or_none()
    if workspace_id is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "workspace_initialization_failed", "title": "Workspace is unavailable"},
        )
    await session.execute(
        text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
        {"workspace_id": str(workspace_id)},
    )
    await session.execute(
        text(
            """
            INSERT INTO workspace_memberships (workspace_id, issuer, subject, role)
            VALUES (:workspace_id, :issuer, :subject, 'owner')
            ON CONFLICT (workspace_id, issuer, subject) DO NOTHING
            """
        ),
        {
            "workspace_id": str(workspace_id),
            "issuer": identity.issuer,
            "subject": identity.subject,
        },
    )
    return str(workspace_id)


async def current_workspace(request: Request) -> AsyncGenerator[WorkspaceContext, None]:
    if SessionFactory is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    identity = getattr(request.state, "identity", None)
    if not identity:
        raise HTTPException(status_code=401, detail="Authentication required")
    async with SessionFactory() as session:
        async with session.begin():
            workspace_id = await initialize_workspace(session, identity)
            context = WorkspaceContext(
                session=session,
                identity=identity,
                workspace_id=workspace_id,
            )
            yield context


async def check_database() -> bool:
    if engine is None:
        return False
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        return result.scalar_one() == 1


async def schedule_workspace_scan(identity: Identity, workspace_id: str) -> None:
    """Signal the tenant-scoped worker without granting it cross-tenant DB access."""
    settings = get_settings()
    if not settings.rag_worker_url or not settings.session_secret:
        return
    now = int(asyncio.get_running_loop().time())
    key = f"{identity.issuer}:{identity.subject}:{workspace_id}"
    previous = _last_scan_signalled.get(key, 0)
    if now - previous < 5:
        return
    _last_scan_signalled[key] = now
    timestamp = str(int(__import__("time").time()))
    path = "/enqueue"
    signature = __import__("hmac").new(
        settings.session_secret.encode("utf-8"),
        f"{timestamp}:POST:{path}:{identity.issuer}:{identity.subject}:{workspace_id}".encode("utf-8"),
        __import__("hashlib").sha256,
    ).hexdigest()

    async def send() -> None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.post(
                    f"{settings.rag_worker_url.rstrip('/')}{path}",
                    headers={
                        "x-rag-issuer": identity.issuer,
                        "x-rag-subject": identity.subject,
                        "x-rag-workspace": workspace_id,
                        "x-rag-timestamp": timestamp,
                        "x-rag-signature": signature,
                    },
                )
        except Exception:
            # Worker outages do not block user-facing requests; the next request re-signals.
            return

    asyncio.create_task(send())


_last_scan_signalled: dict[str, int] = {}
