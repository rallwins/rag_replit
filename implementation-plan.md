# Implementation Plan

## Delivery gate

This plan is ordered. Application implementation begins only after `architecture.md`, `api-specification.md`, `lib/api-spec/openapi.yaml`, and `database/schema.sql` have been reviewed into the project. Those design documents are now the baseline; any contract or schema change must be reflected in the corresponding design document before implementation proceeds.

## Phase 1 — Application contracts and scaffold

- Create a React + TypeScript web artifact, a narrow Express integration edge for Replit Auth/App Storage, and a dedicated Python + FastAPI service/worker layout while preserving the existing workspace conventions.
- Expand the OpenAPI contract to cover health, dashboard/activity, safe config, collections, document upload/lifecycle, retrieval, and grounded answers.
- Generate frontend client/types from OpenAPI before building the UI.
- **Acceptance:** documented routes have validated request/response schemas and the app displays service health/config state without exposing secrets.

## Phase 2 — PostgreSQL and workspace security

- Provision a PostgreSQL database with `pgvector`; apply the schema as a versioned migration, including extension and HNSW index.
- Implement verified Replit Auth session mapping, workspace membership checks, transaction-local workspace scope, least-privilege runtime roles, and RLS.
- Add repositories and transactional persistence for collections, documents, chunks, jobs, activity, and query metrics.
- **Acceptance:** cross-workspace reads/writes are denied at the application and database boundaries; readiness fails clearly when required DB capabilities are absent.

## Phase 3 — Document storage and durable ingestion

- Implement local development storage and private App Storage adapters.
- Issue private upload URLs at the authenticated edge; finalize uploads only after ownership, size/type/content validation, safe names, checksums, duplicate detection, and idempotency checks.
- Implement extraction for PDF, DOCX, Markdown, text, and HTML; record source page/offset metadata where available.
- Chunk by configured token budget/overlap; batch embedding requests; atomically persist chunks, vectors, model metadata, and document/job states.
- Run a separate worker using database leases, bounded concurrency, retry/backoff, failure codes, and cancellation on delete.
- **Acceptance:** a valid upload returns `202`, transitions to ready, and produces searchable chunks; unsupported/corrupt files fail visibly and can be retried when appropriate.

## Phase 4 — Retrieval, generation, and citations

- Implement configurable OpenAI-compatible LLM and embedding adapters, startup configuration validation, timeouts, bounded output, and redacted errors.
- Implement cosine vector search scoped to workspace, collection, and ready documents; expose ranked search results.
- Implement grounded answer generation, no-source behavior, server-resolved citations, and content-free query metrics.
- **Acceptance:** queries retrieve only authorized sources, return citations for retrieved chunks, do not call the LLM when no source meets threshold, and return safe errors on provider outages.

## Phase 5 — React user experience

- Build collection and document management, upload progress/status, search inspection, question/answer and citations, overview/activity, configuration readiness, and error/empty/loading states.
- Use only generated API hooks/types and real API responses; no mock success states for critical flows.
- **Acceptance:** users can navigate core flows with real persisted data, and all API errors or ingestion states have usable feedback.

## Phase 6 — Operational hardening and verification

- Add structured request/worker logs, request IDs, redaction tests, rate/size/concurrency limits, health/readiness behavior, and security headers.
- Add unit and integration tests for chunking/extraction, provider adapters, workspace isolation, job recovery, retrieval/citations, and API validation. Use deterministic provider fakes only in tests.
- Document environment variables, local setup, migrations, worker startup, deployment requirements, backup/restore, and model/dimension changes.
- Run type checks, Python lint/tests, migration checks, API contract/codegen consistency, and manual preview validation after the complete implementation.
- **Acceptance:** services start from documented commands, readiness is accurate, failure paths are explicit, and the production checklist is complete.

## Release blockers

- Replit Auth must be configured before production endpoints accept requests.
- A pgvector-enabled PostgreSQL `DATABASE_URL` is required to run persistence and vector search.
- LLM and embedding provider endpoints/models and their credentials are deployment configuration; secrets must be provided through the environment secrets manager and never committed.
- Replit Auth and private App Storage must be configured before storing real user documents outside local development.
- The initial embedding dimension is fixed at 1536. A different dimension requires an explicit migration and complete re-embedding before switching.
