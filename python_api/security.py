import hashlib
import hmac
import time
from dataclasses import dataclass

from fastapi import Request
from starlette.responses import JSONResponse

from .config import get_settings


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str


def owner_hash(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def internal_signature(timestamp: str, method: str, path: str, *values: str) -> str:
    secret = get_settings().session_secret
    canonical = ":".join((timestamp, method.upper(), path, *values))
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def valid_signature(signature: str, expected: str) -> bool:
    return hmac.compare_digest(signature, expected)


async def identity_middleware(request: Request, call_next):
    if request.url.path in {"/healthz", "/v1/health/ready"}:
        return await call_next(request)

    settings = get_settings()
    if not settings.session_secret:
        return JSONResponse(
            status_code=503,
            media_type="application/problem+json",
            content={
                "type": "about:blank",
                "title": "Authentication gateway is not configured",
                "status": 503,
                "code": "auth_gateway_misconfigured",
                "request_id": request.headers.get("x-request-id", ""),
            },
        )

    subject = request.headers.get("x-rag-subject", "")
    issuer = request.headers.get("x-rag-issuer", "")
    timestamp = request.headers.get("x-rag-timestamp", "")
    signature = request.headers.get("x-rag-signature", "")
    try:
        issued_at = int(timestamp)
    except (TypeError, ValueError):
        issued_at = 0
    query = request.scope.get("query_string", b"").decode("latin-1")
    path = request.url.path
    if query:
        path = f"{path}?{query}"
    canonical = f"{timestamp}:{request.method}:{path}:{issuer}:{subject}"
    expected = hmac.new(
        settings.session_secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if (
        not subject
        or issuer != settings.issuer_url
        or issued_at <= 0
        or abs(time.time() - issued_at) > 60
        or not hmac.compare_digest(signature, expected)
    ):
        return JSONResponse(
            status_code=401,
            media_type="application/problem+json",
            content={
                "type": "about:blank",
                "title": "A valid browser session is required",
                "status": 401,
                "code": "authentication_required",
                "request_id": request.headers.get("x-request-id", ""),
            },
        )
    request.state.identity = Identity(issuer=issuer, subject=subject)
    return await call_next(request)
