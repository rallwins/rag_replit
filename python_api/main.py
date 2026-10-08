import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .config import get_settings
from .db import check_database, engine
from .routes import router
from .security import identity_middleware

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("rag.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    if engine is not None:
        await engine.dispose()


app = FastAPI(
    title="RAG Knowledge Workspace API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.middleware("http")(identity_middleware)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    started = time.perf_counter()
    request_id = request.headers.get("x-request-id", "")
    try:
        response = await call_next(request)
    except Exception as error:
        logger.error(
            "Unhandled request failure",
            extra={
                "request_id": request_id,
                "method": request.method,
                "route": request.url.path,
                "error_type": type(error).__name__,
            },
        )
        raise
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    response.headers["x-request-id"] = request_id
    logger.info(
        "Request completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "route": request.url.path,
            "status": response.status_code,
            "latency_ms": elapsed_ms,
        },
    )
    return response


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, error: HTTPException):
    detail = error.detail
    if isinstance(detail, dict):
        title = str(detail.get("title", "Request failed"))
        code = str(detail.get("code", "request_failed"))
        message = detail.get("detail")
    else:
        title = str(detail) if isinstance(detail, str) else "Request failed"
        code = "request_failed"
        message = None
    return JSONResponse(
        status_code=error.status_code,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": title,
            "status": error.status_code,
            "code": code,
            "request_id": request.headers.get("x-request-id", ""),
            **({"detail": message} if message else {}),
        },
        headers=error.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, error: RequestValidationError):
    errors = [
        {
            "field": ".".join(str(part) for part in item.get("loc", ())),
            "message": str(item.get("msg", "Invalid value")),
        }
        for item in error.errors()
    ]
    return JSONResponse(
        status_code=422,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": "Request validation failed",
            "status": 422,
            "code": "validation_error",
            "request_id": request.headers.get("x-request-id", ""),
            "errors": errors,
        },
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, error: Exception):
    logger.error(
        "Unhandled application error",
        extra={
            "request_id": request.headers.get("x-request-id", ""),
            "route": request.url.path,
            "error_type": type(error).__name__,
        },
    )
    return JSONResponse(
        status_code=500,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": "The request could not be completed",
            "status": 500,
            "code": "internal_error",
            "request_id": request.headers.get("x-request-id", ""),
        },
    )


@app.get("/healthz")
async def liveness():
    return {"status": "ok"}


@app.get("/v1/health/ready")
async def readiness():
    checks: dict[str, str] = {
        "database": "unavailable",
        "vector": "unavailable",
        "embeddings": "misconfigured",
        "llm": "misconfigured",
        "storage": "misconfigured",
        "auth": "misconfigured",
        "worker": "unavailable",
    }
    try:
        if await check_database() and engine is not None:
            checks["database"] = "ok"
            async with engine.connect() as connection:
                installed = await connection.execute(
                    text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
                )
                checks["vector"] = "ok" if installed.scalar_one() else "unavailable"
    except Exception as error:
        logger.warning("Readiness database check failed", extra={"error_type": type(error).__name__})

    if settings.embeddings_configured:
        checks["embeddings"] = "ok"
    if settings.llm_configured:
        checks["llm"] = "ok"
    if (
        settings.rag_internal_storage_base_url
        and os.getenv("PRIVATE_OBJECT_DIR")
        and os.getenv("DEFAULT_OBJECT_STORAGE_BUCKET_ID")
    ):
        checks["storage"] = "ok"
    if settings.session_secret and os.getenv("REPL_ID"):
        checks["auth"] = "ok"
    if settings.rag_worker_url:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                worker = await client.get(f"{settings.rag_worker_url.rstrip('/')}/healthz")
            checks["worker"] = "ok" if worker.status_code == 200 else "unavailable"
        except httpx.HTTPError:
            checks["worker"] = "unavailable"

    ready = all(value == "ok" for value in checks.values())
    body = {"status": "ready" if ready else "not_ready", "checks": checks}
    return JSONResponse(status_code=200 if ready else 503, content=body)


app.include_router(router, prefix="/v1")
