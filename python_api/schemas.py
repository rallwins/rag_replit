from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CollectionCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class CollectionUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class DocumentUploadInput(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=1)
    contentType: str = Field(min_length=1, max_length=255)
    objectPath: str = Field(min_length=1, max_length=2048)


class SearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)
    min_score: float | None = Field(default=None, ge=0, le=1)


class QueryRequest(StrictModel):
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)
    min_score: float | None = Field(default=None, ge=0, le=1)


class CollectionOut(BaseModel):
    id: str
    name: str
    description: str
    document_count: int
    created_at: datetime
    updated_at: datetime


class CollectionPage(BaseModel):
    items: list[CollectionOut]
    next_cursor: str | None = None


class DocumentOut(BaseModel):
    id: str
    collection_id: str
    filename: str
    media_type: str
    byte_size: int
    status: Literal["queued", "processing", "ready", "failed", "deleting", "deleted"]
    failure_code: str | None = None
    chunk_count: int
    page_count: int | None = None
    created_at: datetime
    updated_at: datetime


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    next_cursor: str | None = None


class ActivityEvent(BaseModel):
    id: str
    event_type: str
    resource_type: str | None
    resource_id: str | None
    outcome: Literal["success", "failure"]
    created_at: datetime


class ActivityPage(BaseModel):
    items: list[ActivityEvent]
    next_cursor: str | None = None


class RetrievedChunk(BaseModel):
    chunk_id: str
    document_id: str
    document_name: str
    content: str
    score: float
    page_number: int | None
    start_offset: int | None
    end_offset: int | None


class SearchResponse(BaseModel):
    query: str
    results: list[RetrievedChunk]


class Citation(BaseModel):
    citation_id: str
    chunk_id: str
    document_id: str
    document_name: str
    excerpt: str
    score: float
    page_number: int | None
    start_offset: int | None
    end_offset: int | None


class QueryResponse(BaseModel):
    query_id: str
    answer: str
    citations: list[Citation]
    retrieved_count: int
    outcome: Literal["answered", "no_sources"]
    latency_ms: int


class Problem(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    code: str
    request_id: str
    detail: str | None = None


class ReadinessStatus(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, Literal["ok", "unavailable", "misconfigured"]]
