# API Specification

The normative machine-readable contract is [`lib/api-spec/openapi.yaml`](lib/api-spec/openapi.yaml) (OpenAPI 3.1). It is the source for generated TypeScript client types and validation schemas. This document records its operational conventions.

## Conventions

- Base path: `/api`; versioned routes use `/api/v1`.
- Production endpoints use the secure Replit Auth session cookie. Workspace scope is derived from verified identity and is never accepted from an untrusted request field.
- JSON uses `application/json`; document bytes upload directly to a short-lived signed private App Storage URL.
- Browser auth uses `GET /auth/user`, `GET /login`, `GET /callback`, and `GET /logout`; the React auth helper owns these flows, not the generated API client.
- Errors use a stable problem object: `type`, `title`, `status`, `detail`, `code`, and `request_id`. Validation errors may add field-level `errors`.
- Lists use bounded `limit`/`cursor` pagination. Timestamps are RFC 3339 UTC. UUIDs are opaque identifiers.
- Upload creates an asynchronous ingestion job and returns `202`; clients poll the document resource for status.
- Query results include excerpts and server-resolved citation metadata. A citation points to a chunk that actually participated in retrieval.
- Provider secrets and raw document contents are never returned by settings, health, activity, or error endpoints.

## Endpoint groups

| Group | Routes | Purpose |
| --- | --- | --- |
| Health | `GET /healthz`, `GET /v1/health/ready` | Liveness and dependency readiness |
| Authentication | `GET /auth/user`, `GET /login`, `GET /callback`, `GET /logout` | Replit OIDC sign-in, session state, and sign-out redirects |
| Overview | `GET /v1/dashboard`, `GET /v1/activity` | Counts and recent content-free operational events |
| Configuration | `GET /v1/system/config` | Safe provider/model names and feature readiness only |
| Collections | `GET/POST /v1/collections`, `GET/PATCH/DELETE /v1/collections/{collection_id}` | Manage workspace-scoped knowledge collections |
| Storage | `POST /storage/uploads/request-url` | Request a short-lived upload URL; file bytes go directly to private App Storage |
| Documents | `GET /v1/collections/{collection_id}/documents`, `POST /v1/collections/{collection_id}/documents`, `GET/DELETE /v1/documents/{document_id}`, `POST /v1/documents/{document_id}/retry` | Finalize, monitor, retry, list, and delete source documents |
| Retrieval | `POST /v1/collections/{collection_id}/search` | Return ranked retrieved passages without LLM generation |
| Answers | `POST /v1/collections/{collection_id}/query` | Grounded LLM answer, citations, retrieval and timing metadata |

## Idempotency and limits

- Clients should send `Idempotency-Key` when finalizing a document upload and creating a query. Keys are scoped to workspace and operation and retained for a bounded period.
- Upload URL requests are limited by `UPLOAD_MAX_BYTES`; finalization verifies private object ownership, actual size/type, and collection access before accepting a job.
- Query `top_k` is bounded by the service (default 5, maximum 20); question and filter lengths are bounded.
- `DELETE` is idempotent. Document deletion marks the document unavailable immediately, then removes chunks and private object bytes asynchronously according to retention policy.
- `429` responses include `Retry-After` when throttled. Provider failures map to retryable `503` or `504` codes without exposing upstream bodies or credentials.

## Ingestion states

`queued`, `processing`, `ready`, `failed`, `deleting`, `deleted`.

Only `ready` documents are searchable. Retry creates or re-queues a durable job and increments an attempt counter. The API never reports indexing success before chunk/vector rows commit.

## Citation contract

Each answer citation includes `citation_id`, `document_id`, `document_name`, `chunk_id`, `excerpt`, optional `page_number` and source character offsets, and a retrieval score. The frontend can link the citation to the matching document and excerpt. Citations are assembled by the API from the retrieved rows; model output cannot introduce arbitrary document IDs or URLs.

## Contract change process

Update the OpenAPI document first, then run `pnpm --filter @workspace/api-spec run codegen` before using the changed generated TypeScript API types. Keep FastAPI request and response models aligned with the contract; additive optional fields are preferred over breaking changes.
