import unittest
from io import BytesIO
from unittest.mock import patch

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from python_api.config import Settings
from python_api.ingestion import (
    DocumentExtractionError,
    SourcePage,
    chunk_pages,
    extract_pages,
)


class FakePdfPage:
    def __init__(self, text: str):
        self.text = text

    def extract_text(self):
        return self.text


class FakePdfReader:
    is_encrypted = False

    def __init__(self, page_texts: list[str]):
        self.pages = [FakePdfPage(text) for text in page_texts]


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

    def test_ocr_extracts_scanned_pdf_pages_and_keeps_original_page_numbers(self):
        settings = Settings(pdf_ocr_enabled=True)
        with (
            patch("python_api.ingestion.PdfReader", return_value=FakePdfReader(["", ""])),
            patch("python_api.ingestion.get_settings", return_value=settings),
            patch(
                "python_api.ingestion._run_pdf_ocr",
                return_value={1: "Scanned first page", 2: "Scanned second page"},
            ) as ocr,
        ):
            pages = extract_pages(b"scanned pdf", "scan.pdf", "application/pdf")

        ocr.assert_called_once_with(b"scanned pdf", [1, 2], settings)
        self.assertEqual([page.page_number for page in pages], [1, 2])
        self.assertEqual([page.text for page in pages], ["Scanned first page", "Scanned second page"])
        chunks = chunk_pages(pages)
        self.assertEqual({chunk.page_number for chunk in chunks}, {1, 2})

    def test_ocr_only_runs_for_textless_pages_in_mixed_pdf(self):
        settings = Settings(pdf_ocr_enabled=True)
        with (
            patch(
                "python_api.ingestion.PdfReader",
                return_value=FakePdfReader(["Native text on page one", "", "Native text on page three"]),
            ),
            patch("python_api.ingestion.get_settings", return_value=settings),
            patch(
                "python_api.ingestion._run_pdf_ocr",
                return_value={2: "OCR text on page two"},
            ) as ocr,
        ):
            pages = extract_pages(b"mixed pdf", "mixed.pdf", "application/pdf")

        ocr.assert_called_once_with(b"mixed pdf", [2], settings)
        self.assertEqual([page.page_number for page in pages], [1, 2, 3])
        self.assertEqual(
            [page.text for page in pages],
            ["Native text on page one", "OCR text on page two", "Native text on page three"],
        )
        chunks = chunk_pages(pages)
        self.assertEqual({chunk.page_number for chunk in chunks}, {1, 2, 3})

    def test_ocr_reads_an_image_page_in_a_real_mixed_pdf(self):
        image = Image.new("RGB", (1600, 480), "white")
        ImageDraw.Draw(image).text(
            (55, 170),
            "SCANNED REPORT ALPHA 739",
            fill="black",
            font=ImageFont.load_default(size=78),
        )
        image_data = BytesIO()
        image.save(image_data, format="PNG")

        with pymupdf.open() as document:
            native_page = document.new_page(width=576, height=216)
            native_page.insert_text((72, 72), "Native searchable text page one", fontsize=18)
            scanned_page = document.new_page(width=576, height=216)
            scanned_page.insert_image(scanned_page.rect, stream=image_data.getvalue())
            pdf_data = document.tobytes()

        with patch("python_api.ingestion.get_settings", return_value=Settings(pdf_ocr_enabled=True)):
            pages = extract_pages(pdf_data, "mixed-scan.pdf", "application/pdf")

        self.assertEqual([page.page_number for page in pages], [1, 2])
        self.assertEqual(pages[0].text, "Native searchable text page one")
        self.assertIn("SCANNED REPORT ALPHA 739", pages[1].text)
        self.assertEqual({chunk.page_number for chunk in chunk_pages(pages)}, {1, 2})

    def test_ocr_failure_is_reported_as_a_document_extraction_error(self):
        settings = Settings(pdf_ocr_enabled=True)
        with (
            patch("python_api.ingestion.PdfReader", return_value=FakePdfReader([""])),
            patch("python_api.ingestion.get_settings", return_value=settings),
            patch(
                "python_api.ingestion._run_pdf_ocr",
                side_effect=DocumentExtractionError("ocr_failed"),
            ),
        ):
            with self.assertRaisesRegex(DocumentExtractionError, "ocr_failed"):
                extract_pages(b"unreadable scan", "scan.pdf", "application/pdf")

    def test_disabling_ocr_does_not_invoke_the_ocr_engine(self):
        settings = Settings(pdf_ocr_enabled=False)
        with (
            patch("python_api.ingestion.PdfReader", return_value=FakePdfReader([""])),
            patch("python_api.ingestion.get_settings", return_value=settings),
            patch("python_api.ingestion._run_pdf_ocr") as ocr,
        ):
            with self.assertRaisesRegex(DocumentExtractionError, "no_extractable_text"):
                extract_pages(b"scanned pdf", "scan.pdf", "application/pdf")

        ocr.assert_not_called()

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
