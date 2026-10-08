import unittest

from python_api.ingestion import (
    DocumentExtractionError,
    SourcePage,
    chunk_pages,
    extract_pages,
)


class IngestionTests(unittest.TestCase):
    def test_extracts_text_and_removes_html_script_content(self):
        pages = extract_pages(
            b"<h1>Release notes</h1><script>ignore_this()</script><p>Searchable text</p>",
            "notes.html",
            "text/html",
        )
        self.assertEqual(len(pages), 1)
        self.assertIn("Release notes", pages[0].text)
        self.assertIn("Searchable text", pages[0].text)
        self.assertNotIn("ignore_this", pages[0].text)

    def test_rejects_unknown_file_types(self):
        with self.assertRaisesRegex(DocumentExtractionError, "unsupported_media_type"):
            extract_pages(b"content", "archive.bin", "application/octet-stream")

    def test_rejects_text_without_extractable_content(self):
        with self.assertRaisesRegex(DocumentExtractionError, "no_extractable_text"):
            extract_pages(b" \n\t ", "empty.txt", "text/plain")

    def test_chunks_long_pages_with_bounded_token_counts_and_offsets(self):
        text = "alpha beta gamma delta epsilon " * 400
        chunks = chunk_pages([SourcePage(text=text, page_number=3)])

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.token_count <= 500 for chunk in chunks))
        self.assertTrue(all(chunk.page_number == 3 for chunk in chunks))
        self.assertTrue(all(chunk.start_offset < chunk.end_offset for chunk in chunks))
        self.assertTrue(
            all(left.start_offset < right.start_offset for left, right in zip(chunks, chunks[1:]))
        )


if __name__ == "__main__":
    unittest.main()
