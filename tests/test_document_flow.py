"""End-to-end proof that a privately uploaded document can become a cited answer.

Set RAG_TEST_DATABASE_URL to a disposable PostgreSQL database with pgvector
installed. The test creates and drops its own randomly named schema.
"""

import asyncio
import hashlib
import hmac
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from pathlib import Path
from uuid import uuid4

import psycopg
from sqlalchemy import text
from sqlalchemy.engine import make_url


TEST_DATABASE_URL = os.getenv("RAG_TEST_DATABASE_URL", "")
if not TEST_DATABASE_URL:
    raise unittest.SkipTest(
        "Set RAG_TEST_DATABASE_URL to a disposable PostgreSQL database to run the document-flow integration test."
    )

TEST_SECRET = "document-flow-test-secret-not-used-by-the-application"
TEST_ISSUER = "https://document-flow.integration.test/oidc"
TEST_SCHEMA = f"rag_flow_test_{uuid4().hex}"


def _psycopg_url(url: str) -> str:
    parsed = make_url(url)
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _create_test_schema() -> str:
    schema_sql = (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text()
    schema_sql = "\n".join(
        line for line in schema_sql.splitlines() if not line.lstrip().startswith("--")
    )
    connection_url = _psycopg_url(TEST_DATABASE_URL)
    with psycopg.connect(connection_url) as connection:
        connection.execute(f'CREATE SCHEMA "{TEST_SCHEMA}"')
        connection.execute(f'SET search_path TO "{TEST_SCHEMA}", public')
        for statement in schema_sql.split(";"):
            if statement.strip():
                connection.execute(statement)

    parsed = make_url(TEST_DATABASE_URL)
    options = f"-csearch_path={TEST_SCHEMA},public"
    return parsed.set(
        drivername="postgresql+psycopg",
        query={**parsed.query, "options": options},
    ).render_as_string(hide_password=False)


try:
    TEST_ENGINE_DATABASE_URL = _create_test_schema()
except Exception as error:
    raise RuntimeError(
        "Could not create the isolated integration-test schema. "
        "Check RAG_TEST_DATABASE_URL, PostgreSQL access, and pgvector availability."
    ) from error

# Configure the real database engine before importing the application modules.
os.environ["DATABASE_URL"] = TEST_ENGINE_DATABASE_URL
os.environ["SESSION_SECRET"] = TEST_SECRET
os.environ["ISSUER_URL"] = TEST_ISSUER

from python_api import db, main, providers, routes, security, storage, worker  # noqa: E402
from python_api.config import Settings  # noqa: E402
from python_api.security import Identity, owner_hash  # noqa: E402


class MockServiceState:
    def __init__(self):
        self.lock = threading.Lock()
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.deleted_objects: set[str] = set()
        self.fail_embeddings = False
        self.embedding_requests: list[list[str]] = []
        self.completion_requests: list[dict] = []
        self.answer = "The records must be kept for seven years. [1]"


class MockServiceHandler(BaseHTTPRequestHandler):
    state: MockServiceState

    def log_message(self, _format: str, *_args) -> None:
        return

    def _reply(self, status: int, payload: bytes = b"", content_type: str = "application/json") -> None:
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _json(self, status: int, value: dict) -> None:
        self._reply(status, json.dumps(value).encode(), "application/json")

    def _valid_storage_signature(self, storage_path: str) -> bool:
        subject = self.headers.get("x-rag-subject", "")
        timestamp = self.headers.get("x-rag-timestamp", "")
        signature = self.headers.get("x-rag-signature", "")
        owner = storage_path.split("/")[1] if "/" in storage_path else ""
        if not subject or owner != owner_hash(subject) or not timestamp.isdigit():
            return False
        if abs(time.time() - int(timestamp)) > 60:
            return False
        canonical = (
            f"{timestamp}:{self.command}:/api/internal/storage/objects/"
            f"{storage_path}:{owner}:{subject}"
        )
        expected = hmac.new(TEST_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def do_GET(self) -> None:
        prefix = "/api/internal/storage/objects/"
        if not self.path.startswith(prefix):
            self._reply(404)
            return
        storage_path = self.path.removeprefix(prefix)
        if not self._valid_storage_signature(storage_path):
            self._reply(403)
            return
        with self.state.lock:
            item = self.state.objects.get(storage_path)
        if item is None:
            self._reply(404)
            return
        content, media_type = item
        self._reply(200, content, media_type)

    def do_DELETE(self) -> None:
        prefix = "/api/internal/storage/objects/"
        if not self.path.startswith(prefix):
            self._reply(404)
            return
        storage_path = self.path.removeprefix(prefix)
        if not self._valid_storage_signature(storage_path):
            self._reply(403)
            return
        with self.state.lock:
            self.state.objects.pop(storage_path, None)
            self.state.deleted_objects.add(storage_path)
        self._reply(204)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._reply(400)
            return

        if self.path == "/v1/embeddings":
            if self.headers.get("authorization") != "Bearer fake-embedding-key":
                self._reply(401)
                return
            inputs = body.get("input")
            if not isinstance(inputs, list) or not all(isinstance(item, str) for item in inputs):
                self._reply(400)
                return
            with self.state.lock:
                self.state.embedding_requests.append(inputs)
                should_fail = self.state.fail_embeddings
            if should_fail:
                self._json(503, {"error": {"message": "temporary test failure"}})
                return
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": index, "embedding": [1.0] + [0.0] * 1535}
                        for index, _ in enumerate(inputs)
                    ],
                    "model": "integration-embedding-model",
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )
            return

        if self.path == "/v1/chat/completions":
            if self.headers.get("authorization") != "Bearer fake-llm-key":
                self._reply(401)
                return
            with self.state.lock:
                self.state.completion_requests.append(body)
                answer = self.state.answer
            self._json(
                200,
                {
                    "id": "chatcmpl-integration-test",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}}],
                    "usage": {"prompt_tokens": 31, "completion_tokens": 12},
                },
            )
            return

        self._reply(404)


def _signed_identity_headers(subject: str, method: str, path: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    canonical = f"{timestamp}:{method.upper()}:{path}:{TEST_ISSUER}:{subject}"
    signature = hmac.new(TEST_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "x-rag-issuer": TEST_ISSUER,
        "x-rag-subject": subject,
        "x-rag-timestamp": timestamp,
        "x-rag-signature": signature,
    }


class DocumentFlowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = MockServiceState()
        handler = type("BoundMockServiceHandler", (MockServiceHandler,), {"state": cls.state})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.settings = Settings(
            database_url=TEST_ENGINE_DATABASE_URL,
            session_secret=TEST_SECRET,
            issuer_url=TEST_ISSUER,
            llm_base_url=f"{base_url}/v1",
            llm_model="integration-completion-model",
            llm_api_key="fake-llm-key",
            llm_timeout_seconds=5,
            embedding_base_url=f"{base_url}/v1",
            embedding_model="integration-embedding-model",
            embedding_api_key="fake-embedding-key",
            embedding_dimensions=1536,
            embedding_timeout_seconds=5,
            retrieval_min_score=0,
            rag_internal_storage_base_url=f"{base_url}/api/internal/storage",
            rag_worker_url="",
        )
        cls.patches = [
            mock.patch.object(module, "get_settings", return_value=cls.settings)
            for module in (providers, routes, security, storage, db)
        ]
        cls.patches.append(mock.patch.object(worker, "settings", cls.settings))
        for active_patch in cls.patches:
            active_patch.start()

    @classmethod
    def tearDownClass(cls):
        for active_patch in reversed(cls.patches):
            active_patch.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)
        if db.engine is not None:
            asyncio.run(db.engine.dispose())
        with psycopg.connect(_psycopg_url(TEST_DATABASE_URL)) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE')

    async def asyncSetUp(self):
        import httpx

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app),
            base_url="http://testserver",
        )
        with self.state.lock:
            self.state.objects.clear()
            self.state.deleted_objects.clear()
            self.state.fail_embeddings = False
            self.state.embedding_requests.clear()
            self.state.completion_requests.clear()

    async def asyncTearDown(self):
        await self.client.aclose()

    async def request_as(self, subject: str, method: str, path: str, **kwargs):
        headers = _signed_identity_headers(subject, method, path)
        headers.update(kwargs.pop("headers", {}))
        return await self.client.request(method, path, headers=headers, **kwargs)

    async def create_collection(self, subject: str, name: str) -> dict:
        response = await self.request_as(
            subject,
            "POST",
            "/v1/collections",
            json={"name": name, "description": ""},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def upload_text(self, subject: str, collection_id: str, name: str, content: str) -> dict:
        owner = owner_hash(subject)
        object_id = str(uuid4())
        object_path = f"/objects/uploads/{owner}/{object_id}"
        storage_path = f"uploads/{owner}/{object_id}"
        with self.state.lock:
            self.state.objects[storage_path] = (content.encode(), "text/plain")

        response = await self.request_as(
            subject,
            "POST",
            f"/v1/collections/{collection_id}/documents",
            json={
                "name": name,
                "size": len(content.encode()),
                "contentType": "text/plain",
                "objectPath": object_path,
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    async def workspace_id(self, identity: Identity) -> str:
        self.assertIsNotNone(db.SessionFactory)
        async with db.SessionFactory() as session:
            async with session.begin():
                return await db.initialize_workspace(session, identity)

    async def set_job_attempt_limit(self, identity: Identity, workspace_id: str, document_id: str):
        async with worker.tenant_transaction(identity, workspace_id) as session:
            await session.execute(
                text(
                    """
                    UPDATE ingestion_jobs
                    SET max_attempts = 1
                    WHERE workspace_id = :workspace_id AND document_id = :document_id
                    """
                ),
                {"workspace_id": workspace_id, "document_id": document_id},
            )

    async def test_upload_ingest_retrieve_answer_retry_isolation_and_delete(self):
        tenant_a = "integration-tenant-alpha"
        tenant_b = "integration-tenant-beta"
        identity_a = Identity(TEST_ISSUER, tenant_a)
        identity_b = Identity(TEST_ISSUER, tenant_b)
        content_a = (
            "The Acme retention policy requires records to be kept for seven years "
            "from their creation date."
        )
        content_b = "Tenant Beta confidential document: the private project code is BLUE-ORCHID."

        unauthenticated = await self.client.get("/v1/collections")
        self.assertEqual(unauthenticated.status_code, 401)

        collection_a = await self.create_collection(tenant_a, "Alpha records")
        collection_b = await self.create_collection(tenant_b, "Beta records")
        document_a = await self.upload_text(
            tenant_a, collection_a["id"], "retention-policy.txt", content_a
        )
        document_b = await self.upload_text(
            tenant_b, collection_b["id"], "beta-private.txt", content_b
        )
        self.assertEqual(document_a["status"], "queued")
        self.assertEqual(document_b["status"], "queued")

        workspace_a = await self.workspace_id(identity_a)
        workspace_b = await self.workspace_id(identity_b)

        # A terminal provider failure makes the real retry endpoint eligible.
        await self.set_job_attempt_limit(identity_a, workspace_a, document_a["id"])
        with self.state.lock:
            self.state.fail_embeddings = True
        failed_job = await worker._claim_job(identity_a, workspace_a)
        self.assertIsNotNone(failed_job)
        await worker._process_job(identity_a, workspace_a, failed_job)

        failed_document = await self.request_as(
            tenant_a, "GET", f"/v1/documents/{document_a['id']}"
        )
        self.assertEqual(failed_document.status_code, 200, failed_document.text)
        self.assertEqual(failed_document.json()["status"], "failed")
        self.assertEqual(failed_document.json()["failure_code"], "provider_unavailable")

        with self.state.lock:
            self.state.fail_embeddings = False
        retried = await self.request_as(
            tenant_a, "POST", f"/v1/documents/{document_a['id']}/retry"
        )
        self.assertEqual(retried.status_code, 202, retried.text)
        self.assertEqual(retried.json()["status"], "queued")

        await worker._run_workspace(identity_a, workspace_a)
        await worker._run_workspace(identity_b, workspace_b)

        for subject, document in ((tenant_a, document_a), (tenant_b, document_b)):
            ready = await self.request_as(subject, "GET", f"/v1/documents/{document['id']}")
            self.assertEqual(ready.status_code, 200, ready.text)
            self.assertEqual(ready.json()["status"], "ready")
            self.assertGreater(ready.json()["chunk_count"], 0)

        search_a = await self.request_as(
            tenant_a,
            "POST",
            f"/v1/collections/{collection_a['id']}/search",
            json={"query": "What is the retention period?"},
        )
        self.assertEqual(search_a.status_code, 200, search_a.text)
        self.assertEqual(len(search_a.json()["results"]), 1)
        self.assertIn(content_a, search_a.json()["results"][0]["content"])
        self.assertNotIn("BLUE-ORCHID", search_a.text)

        # A signed-in tenant still cannot address another tenant's collection.
        cross_tenant_search = await self.request_as(
            tenant_a,
            "POST",
            f"/v1/collections/{collection_b['id']}/search",
            json={"query": "What is the private project code?"},
        )
        self.assertEqual(cross_tenant_search.status_code, 404)

        answer = await self.request_as(
            tenant_a,
            "POST",
            f"/v1/collections/{collection_a['id']}/query",
            json={"question": "How long must the records be kept?"},
        )
        self.assertEqual(answer.status_code, 200, answer.text)
        answer_body = answer.json()
        self.assertEqual(answer_body["outcome"], "answered")
        self.assertEqual(answer_body["answer"], self.state.answer)
        self.assertEqual(answer_body["retrieved_count"], 1)
        self.assertEqual(len(answer_body["citations"]), 1)
        citation = answer_body["citations"][0]
        retrieved_chunk = search_a.json()["results"][0]
        self.assertEqual(citation["document_id"], document_a["id"])
        self.assertEqual(citation["chunk_id"], retrieved_chunk["chunk_id"])
        self.assertEqual(citation["document_name"], "retention-policy.txt")
        self.assertEqual(citation["excerpt"], retrieved_chunk["content"][:500])
        self.assertIn("seven years", citation["excerpt"])
        self.assertEqual(citation["citation_id"], "1")
        self.assertGreater(citation["score"], 0)
        with self.state.lock:
            self.assertTrue(self.state.embedding_requests)
            self.assertEqual(len(self.state.completion_requests), 1)
            prompt = self.state.completion_requests[0]["messages"][1]["content"]
        self.assertIn(content_a, prompt)
        self.assertNotIn("BLUE-ORCHID", prompt)

        delete_response = await self.request_as(
            tenant_a, "DELETE", f"/v1/documents/{document_a['id']}"
        )
        self.assertEqual(delete_response.status_code, 204)
        await worker._run_workspace(identity_a, workspace_a)
        deleted = await self.request_as(
            tenant_a, "GET", f"/v1/documents/{document_a['id']}"
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["status"], "deleted")

        no_sources = await self.request_as(
            tenant_a,
            "POST",
            f"/v1/collections/{collection_a['id']}/query",
            json={"question": "How long must the records be kept?"},
        )
        self.assertEqual(no_sources.status_code, 200, no_sources.text)
        self.assertEqual(no_sources.json()["outcome"], "no_sources")
        self.assertEqual(no_sources.json()["citations"], [])

        owner_a = owner_hash(tenant_a)
        with self.state.lock:
            deleted_paths = list(self.state.deleted_objects)
            self.assertEqual(len(deleted_paths), 1)
            self.assertTrue(deleted_paths[0].startswith(f"uploads/{owner_a}/"))
            self.assertEqual(len(self.state.objects), 1)
        still_ready_for_b = await self.request_as(
            tenant_b, "GET", f"/v1/documents/{document_b['id']}"
        )
        self.assertEqual(still_ready_for_b.status_code, 200)
        self.assertEqual(still_ready_for_b.json()["status"], "ready")


if __name__ == "__main__":
    unittest.main()
