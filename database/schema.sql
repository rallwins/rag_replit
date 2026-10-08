-- Production RAG application, initial PostgreSQL + pgvector schema.
-- Run as a migration using a privileged migration role, not the runtime role.
-- The initial vector dimension is 1536; see architecture.md before changing it.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Replit Auth web sessions are opaque IDs; bearer tokens are never stored in
-- the browser. The API server stores session payloads server-side.
CREATE TABLE sessions (
    sid varchar PRIMARY KEY,
    sess jsonb NOT NULL,
    expire timestamp NOT NULL
);
CREATE INDEX idx_session_expire ON sessions (expire);

CREATE TABLE users (
    id varchar PRIMARY KEY DEFAULT gen_random_uuid()::text,
    email varchar UNIQUE,
    first_name varchar,
    last_name varchar,
    profile_image_url varchar,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workspaces (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id text NOT NULL UNIQUE,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workspace_memberships (
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    issuer text NOT NULL,
    subject text NOT NULL,
    role text NOT NULL DEFAULT 'member'
        CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, issuer, subject)
);
CREATE INDEX workspace_memberships_subject_idx
    ON workspace_memberships (issuer, subject);

CREATE TABLE collections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    UNIQUE (workspace_id, id)
);
CREATE INDEX collections_workspace_updated_idx
    ON collections (workspace_id, updated_at DESC)
    WHERE deleted_at IS NULL;

CREATE TABLE documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    collection_id uuid NOT NULL,
    original_filename text NOT NULL,
    media_type text NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    sha256 char(64) NOT NULL,
    storage_key text NOT NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'processing', 'ready', 'failed', 'deleting', 'deleted')),
    failure_code text,
    page_count integer CHECK (page_count IS NULL OR page_count >= 0),
    chunk_count integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    embedding_provider text,
    embedding_model text,
    embedding_dimensions integer,
    uploaded_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    UNIQUE (workspace_id, id),
    UNIQUE (workspace_id, collection_id, sha256),
    FOREIGN KEY (workspace_id, collection_id)
        REFERENCES collections (workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX documents_collection_status_idx
    ON documents (workspace_id, collection_id, status, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE TABLE document_chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    collection_id uuid NOT NULL,
    document_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    content text NOT NULL CHECK (length(content) > 0),
    token_count integer NOT NULL CHECK (token_count > 0),
    page_number integer CHECK (page_number IS NULL OR page_number > 0),
    start_offset integer CHECK (start_offset IS NULL OR start_offset >= 0),
    end_offset integer CHECK (end_offset IS NULL OR end_offset >= start_offset),
    embedding vector(1536) NOT NULL,
    embedding_model text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, document_id, ordinal),
    FOREIGN KEY (workspace_id, collection_id)
        REFERENCES collections (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, document_id)
        REFERENCES documents (workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX document_chunks_scope_idx
    ON document_chunks (workspace_id, collection_id, document_id);
CREATE INDEX document_chunks_embedding_hnsw_idx
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Durable ingestion queue. A worker claims eligible rows with
-- FOR UPDATE SKIP LOCKED and uses lease_until to recover abandoned jobs.
CREATE TABLE ingestion_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    document_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'processing', 'succeeded', 'failed', 'cancelled')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    worker_id text,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, document_id)
        REFERENCES documents (workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX ingestion_jobs_claim_idx
    ON ingestion_jobs (available_at, created_at)
    WHERE status IN ('queued', 'processing');
CREATE INDEX ingestion_jobs_document_idx
    ON ingestion_jobs (workspace_id, document_id, created_at DESC);

CREATE TABLE activity_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    actor_subject text,
    event_type text NOT NULL,
    resource_type text,
    resource_id uuid,
    request_id text,
    outcome text NOT NULL CHECK (outcome IN ('success', 'failure')),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX activity_events_workspace_time_idx
    ON activity_events (workspace_id, created_at DESC);

CREATE TABLE query_metrics (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    collection_id uuid NOT NULL,
    actor_subject text,
    request_id text NOT NULL,
    llm_provider text,
    llm_model text,
    embedding_provider text,
    embedding_model text,
    retrieved_count integer NOT NULL DEFAULT 0 CHECK (retrieved_count >= 0),
    prompt_tokens integer CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
    completion_tokens integer CHECK (completion_tokens IS NULL OR completion_tokens >= 0),
    retrieval_ms integer NOT NULL CHECK (retrieval_ms >= 0),
    generation_ms integer CHECK (generation_ms IS NULL OR generation_ms >= 0),
    outcome text NOT NULL CHECK (outcome IN ('success', 'no_sources', 'failure')),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, collection_id)
        REFERENCES collections (workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX query_metrics_workspace_time_idx
    ON query_metrics (workspace_id, created_at DESC);

-- The API and worker set app.workspace_id transaction-locally after verifying
-- identity and membership. Runtime roles must not own these tables or BYPASSRLS.
ALTER TABLE collections ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE activity_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE query_metrics ENABLE ROW LEVEL SECURITY;

ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
ALTER TABLE workspace_memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE collections FORCE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE ingestion_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE activity_events FORCE ROW LEVEL SECURITY;
ALTER TABLE query_metrics FORCE ROW LEVEL SECURITY;

CREATE POLICY workspaces_identity_isolation ON workspaces
    USING (
        external_id =
        current_setting('app.issuer', true) || ':' ||
        current_setting('app.subject', true)
    )
    WITH CHECK (
        external_id =
        current_setting('app.issuer', true) || ':' ||
        current_setting('app.subject', true)
    );
CREATE POLICY workspace_memberships_identity_isolation ON workspace_memberships
    USING (
        issuer = current_setting('app.issuer', true)
        AND subject = current_setting('app.subject', true)
    )
    WITH CHECK (
        issuer = current_setting('app.issuer', true)
        AND subject = current_setting('app.subject', true)
    );
CREATE POLICY collections_workspace_isolation ON collections
    USING (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY documents_workspace_isolation ON documents
    USING (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY chunks_workspace_isolation ON document_chunks
    USING (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY jobs_workspace_isolation ON ingestion_jobs
    USING (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY activity_workspace_isolation ON activity_events
    USING (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid);
CREATE POLICY query_metrics_workspace_isolation ON query_metrics
    USING (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid)
    WITH CHECK (workspace_id = nullif(current_setting('app.workspace_id', true), '')::uuid);
