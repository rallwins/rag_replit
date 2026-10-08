# Production RAG Application — Architecture

## Purpose and scope

Build a workspace-scoped document question-answering application. Users upload files into a collection, track ingestion, search the indexed passages, and ask questions answered from retrieved evidence with inspectable citations.

The first release supports PDF, DOCX, Markdown, plain text, and HTML documents. It does not promise OCR for scanned PDFs, internet crawling, cross-workspace search, or model fine-tuning.

## System context

```text
Browser (React + TypeScript)
   │ same-origin /api, secure session cookie
   ▼
Express edge adapter ─── FastAPI application ───── PostgreSQL + pgvector
   │                         │                       ├─ workspaces, documents, chunks, audit events
   │                         │                       └─ durable ingestion job queue (SKIP LOCKED)
   │                         ├─ LLM provider adapter ─ configurable OpenAI-compatible chat endpoint
   │                         ├─ embedding adapter ─── configurable OpenAI-compatible embedding endpoint
   │                         └─ private object storage adapter
   ├─ Replit-managed OIDC session
   ├─ private App Storage URL signing
   └─ same-origin API proxy
                              ▲
                     separate ingestion worker
                              │
                              └──── App Storage + provider APIs
```

The Express edge is a narrow Replit integration adapter: it owns browser OIDC sessions, private App Storage signed-upload URLs, and same-origin proxying. FastAPI owns the RAG API, validation, workspace scoping, retrieval, prompt construction, citations, provider calls, and ingestion. The Python worker uses the same repositories and adapters as FastAPI. The browser never receives provider credentials and never calls model providers directly.

## Components and responsibilities

### Frontend

- React, TypeScript, Vite, and generated API types from `lib/api-spec/openapi.yaml`.
- Calls the API through the same-origin `/api` path; never hardcodes service ports or credentials.
- Presents collections, document ingestion status, retrieval results, answers, and source citations.
- Treats API errors and asynchronous document states as explicit UI states; does not simulate successful uploads or answers.

### Express edge adapter

- Keeps the framework-specific Replit Auth and App Storage integrations at the HTTP edge.
- Uses the Replit Auth PKCE web flow and secure, HTTP-only session cookies; it does not implement passwords or client-side token storage.
- Creates short-lived upload URLs for private App Storage after authentication and validates file metadata and user quota.
- Proxies `/api/v1/*` requests to the private FastAPI service and forwards verified identity using trusted internal headers. It strips client-supplied identity headers.
- The FastAPI service binds to loopback or an otherwise private service address; it is not directly exposed to untrusted clients.

### FastAPI API

- Python 3.12+ and FastAPI, with Pydantic request/response validation.
- Stateless request handlers; verified edge identity is mapped to a personal workspace, and workspace scoping is applied to every data access.
- Document finalization routes verify ownership of the App Storage object, create a document record and durable ingestion job, and return `202 Accepted`.
- Query routes embed the question, perform workspace- and collection-scoped vector search, apply a score threshold, and pass only retrieved passages to the configured LLM.
- Citations are generated from retrieved chunk records, not trusted to model-generated source identifiers.
- Health endpoints separate process liveness from dependency readiness.

### Ingestion worker

- Runs independently from API request workers and claims queued jobs in short PostgreSQL transactions using `FOR UPDATE SKIP LOCKED`.
- State transitions are persisted (`queued → processing → ready|failed`); failures retain a bounded, sanitized error code and can be retried.
- Reads source bytes from private App Storage, extracts text and page/paragraph metadata, chunks by token budget with overlap, batches embeddings, and writes chunks and vectors atomically.
- Uses an expiring lease and attempt counter so abandoned work can be reclaimed safely. Deleting a document invalidates pending work.
- Limits concurrent work, provider request sizes, and retry/backoff; does not silently mark failed ingestion as ready.

### Persistence and files

- PostgreSQL is the source of truth. `pgvector` stores chunk embeddings; HNSW cosine search is used for approximate nearest-neighbor lookup.
- Database schema and initial release index dimension are specified in `database/schema.sql`. The initial vector column is 1536 dimensions.
- App Storage holds private original files using opaque object paths; the database stores only object paths and searchable metadata. Local development storage stays outside the web root. Use private bucket ACLs and lifecycle/backup policies.
- Database transactions use a transaction-local workspace identifier. Row-level security is a defense-in-depth boundary, not a replacement for authorization checks.
- A PostgreSQL-backed job table avoids losing queued ingestion work when the API process restarts and avoids adding Redis as a first-release dependency.

## Identity, tenancy, and authorization

- Production browser requests use a verified Replit Auth PKCE session cookie. The Express edge validates the session; it does not implement local passwords or expose session tokens to client JavaScript.
- A trusted edge identity is mapped to a personal workspace, with a membership row for access checks. An organization adapter can replace this mapping later.
- All collections, documents, chunks, jobs, and activity records are workspace-owned. IDs are not authorization credentials.
- Development-only identity bypass must be explicitly enabled, must refuse non-local deployment settings, and must never be the production default.
- Production launch is blocked until Replit Auth is configured and verified.

## Provider configuration

Provider adapters are selected using environment variables, so provider choices and secrets are never stored in application tables or returned by configuration endpoints.

### LLM

- `LLM_PROVIDER`: adapter identifier; initial implementation supports `openai_compatible`.
- `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_OUTPUT_TOKENS`.
- The adapter uses a bounded request timeout, bounded output, no automatic cross-provider fallback, and sanitized error mapping.

### Embeddings

- `EMBEDDING_PROVIDER`: adapter identifier; initial implementation supports `openai_compatible`.
- `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_API_KEY`, `EMBEDDING_DIMENSIONS`.
- `EMBEDDING_DIMENSIONS` must be `1536` for the initial schema. Changing model names is allowed only when the model returns that dimension and the vector space remains compatible. Changing dimensions or vector spaces requires a migration, re-embedding, and index rebuild before switching query traffic.
- LLM and embedding credentials may be separate. Secrets are supplied through the deployment's secrets manager, not committed files.

### Other settings

- `DATABASE_URL`, `SESSION_SECRET`, `APP_ENV`.
- `PRIVATE_OBJECT_DIR` and App Storage bucket configuration are provisioned by the platform integration; `DOCUMENT_STORAGE_PATH` is for local development only.
- `UPLOAD_MAX_BYTES`, `CHUNK_SIZE_TOKENS`, `CHUNK_OVERLAP_TOKENS`, `RETRIEVAL_TOP_K_DEFAULT`, and `RETRIEVAL_MIN_SCORE`.
- The application validates configuration at startup, reports missing/invalid non-secret setting names in logs, and never logs credential values.

## Request and ingestion flows

### Upload and indexing

1. Authenticate and resolve workspace; request a short-lived private App Storage upload URL with bounded file metadata.
2. Browser uploads bytes directly to the signed URL. Validate file size, extension, MIME type, and detected content before queueing work.
3. Finalize the upload through FastAPI, verify the object belongs to the authenticated user, then store a document record plus queued job. Return `202`.
4. Worker claims the job, extracts text, preserves page/paragraph information, chunks with overlap, batches embedding requests, and commits chunks plus ready status.
5. On failure, persist a sanitized failure code and status; expose an explicit retry operation.

### Question and answer

1. Authenticate, validate question length and collection access.
2. Embed the question using the configured embedding model.
3. Search ready chunks in the authorized collection with cosine distance; return the best bounded set above the configured threshold.
4. If nothing meets the threshold, return a grounded "no relevant sources found" answer without calling the LLM.
5. Otherwise build a bounded context containing chunk IDs and text, call the configured LLM, and return the answer with citations resolved from the retrieved chunk rows.
6. Record operational metadata (latency, counts, status, provider/model identifiers, request ID) without storing question/answer text by default.

## Security, privacy, and reliability

- Enforce request, upload, page-count, extracted-text, chunk, question, provider-timeout, and output-token limits.
- Accept an explicit allowlist of document types; sanitize filenames; keep objects private; use opaque storage keys; reject path traversal and archive expansion attacks.
- Apply per-workspace quotas and rate limits at the API edge before production exposure.
- Use parameterized SQL, strict workspace predicates, RLS, least-privilege database credentials, and TLS for external connections.
- Do not log session cookies, provider keys, raw document text, prompts, or full answers. Logs use request IDs and sanitized structured fields.
- Use structured JSON logs with severity, request ID, workspace ID (non-secret), route, duration, outcome, and error code. Keep content-free audit events separate from application logs.
- Use readiness probes for PostgreSQL, required `pgvector` extension, provider configuration, and storage reachability. Liveness checks only that the process is responsive.
- Database backups, private bucket backups/lifecycle, restore drills, alerting, and retention policy are deployment responsibilities and release gates.

## Observability

- Emit structured request and worker logs with correlation IDs.
- Track request/ingestion/query latency, upload and extraction failures, queue age/depth, provider timeouts/errors, retrieval counts, token usage when available, and database health.
- Persist a limited content-free activity stream for user-visible ingestion and query outcomes.
- Expose `/healthz` for liveness and `/v1/health/ready` for readiness; do not expose configuration secrets or detailed infrastructure diagnostics.

## Key decisions and trade-offs

1. **PostgreSQL job queue first.** It makes ingestion durable without introducing Redis; high-throughput installations can replace the queue behind the same job interface.
2. **One vector dimension per deployment.** A 1536-dimensional initial column supports a practical range of providers/models but requires re-embedding and an index migration when dimensions or vector spaces change.
3. **OIDC delegated, not local accounts.** Replit Auth provides the browser PKCE/session flow; no local passwords or client-side token storage are introduced.
4. **OpenAI-compatible provider adapters first.** This gives users a configurable endpoint/model/key while keeping the provider interface replaceable; native vendor-specific features are not assumed.
5. **Private object storage in production.** App Storage holds private originals; PostgreSQL holds metadata and vectors, not large file bytes.

## Deployment topology

Deploy the React static frontend, Express edge adapter, FastAPI API, and ingestion worker as separate processes/services. They share the same API base path and PostgreSQL database; the worker additionally needs private storage and provider access. Production topology must provide a pgvector-enabled PostgreSQL service, private App Storage, secret injection, HTTPS, health checks, and configured Replit Auth.
