# Python tests

Run the fast extraction and chunking tests with:

```sh
uv run --locked python -m unittest discover -s tests -v
```

Run the document-to-cited-answer integration test with:

```sh
RAG_TEST_DATABASE_URL='postgresql://…' \
  uv run --locked python -m unittest discover -s tests -p 'test_document_flow.py' -v
```

`RAG_TEST_DATABASE_URL` must point to a disposable PostgreSQL database with
`pgvector` available. Do not point it at production or a database whose data
must be preserved. The integration test creates a randomly named schema, runs
the real API identity middleware, route handlers, database policies, and
ingestion worker against that schema, then drops it. Its private-storage,
embedding, and completion endpoints are local HTTP mocks; no external provider
or object-storage credentials are used. If the variable is not set, the
integration test is skipped during test discovery.
