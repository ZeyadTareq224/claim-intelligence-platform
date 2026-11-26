from unittest.mock import patch
import subprocess

import pymupdf
import pytest

from app.services.pdf_extraction import OcrError, PdfExtractor, TesseractOcr, usable_text


class FakeOcr:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def recognize(self, image):
        self.calls += 1
        assert image.startswith(b"\x89PNG")
        if self.error:
            raise OcrError(self.error)
        return [("Scanned", (10, 10, 100, 40), 91.0)]


def make_document():
    doc = pymupdf.open()
    digital = doc.new_page()
    digital.insert_text((72, 72), "Policy number P-123")
    temp = pymupdf.open()
    image_page = temp.new_page(width=300, height=100)
    image_page.insert_text((10, 50), "Scanned page", fontsize=20)
    image = image_page.get_pixmap().tobytes("png")
    scanned = doc.new_page()
    scanned.insert_image(pymupdf.Rect(30, 30, 330, 130), stream=image)
    doc.new_page()
    temp.close()
    return doc


def test_mixed_document_routes_per_page_and_keeps_coordinates(tmp_path):
    doc = make_document()
    path = tmp_path / "mixed.pdf"
    doc.save(path)
    doc.close()
    ocr = FakeOcr()
    pages = PdfExtractor(ocr).extract(path)
    assert [p.status for p in pages] == ["completed", "completed", "blank"]
    assert [p.method for p in pages] == ["native", "ocr", None]
    assert [p.page_number for p in pages] == [1, 2, 3]
    assert "P-123" in pages[0].blocks[0].text and pages[0].blocks[0].bbox
    assert pages[1].blocks[0].text == "Scanned" and pages[1].blocks[0].confidence == 91
    assert ocr.calls == 1


def test_image_region_uses_mixed_and_preserves_native():
    doc = make_document()
    doc[0].insert_image(pymupdf.Rect(50, 150, 350, 250), stream=doc[1].get_pixmap().tobytes("png"))
    ocr = FakeOcr()
    page = PdfExtractor(ocr).extract_page(doc[0])
    assert page.method == "mixed" and [block.method for block in page.blocks] == ["native", "ocr"]
    assert ocr.calls == 1
    doc.close()


def test_ocr_failure_keeps_other_pages_and_native_text():
    doc = make_document()
    doc[0].insert_image(pymupdf.Rect(50, 150, 350, 250), stream=doc[1].get_pixmap().tobytes("png"))
    extractor = PdfExtractor(FakeOcr("ocr_timeout"))
    first, second, third = [extractor.extract_page(page) for page in doc]
    assert first.status == "failed" and first.error == "ocr_timeout" and first.blocks[0].method == "native"
    assert second.status == "failed" and second.error == "ocr_timeout"
    assert third.status == "blank"
    doc.close()


def test_existing_text_layer_prevents_ocr():
    doc = make_document()
    doc[1].insert_text((40, 60), "Scanned page with existing text layer")
    ocr = FakeOcr()
    page = PdfExtractor(ocr).extract_page(doc[1])
    assert page.method == "native" and ocr.calls == 0
    doc.close()


def test_rejects_unusable_text_and_bounds_rendering():
    assert not usable_text("\ufffd\ufffd\ufffd")
    assert usable_text("P-123")
    doc = make_document()
    result = PdfExtractor(FakeOcr(), max_pixels=1).extract_page(doc[1])
    assert result.error == "ocr_image_too_large"
    doc.close()


@pytest.mark.parametrize("failure,code", [(FileNotFoundError(), "ocr_unavailable"), (subprocess.TimeoutExpired("tesseract", 1), "ocr_timeout"), (subprocess.CalledProcessError(1, "tesseract"), "ocr_failed")])
def test_tesseract_errors_are_categorized(failure, code):
    with patch("app.services.pdf_extraction.subprocess.run", side_effect=failure), pytest.raises(OcrError, match=code):
        TesseractOcr().recognize(b"png")


def test_ocr_language_is_validated():
    with pytest.raises(ValueError):
        TesseractOcr(language="eng;bad")


def test_blank_scanned_page_is_classified_without_ocr():
    source = pymupdf.open()
    white_page = source.new_page(width=200, height=200)
    image = white_page.get_pixmap().tobytes("png")
    document = pymupdf.open()
    page = document.new_page(width=200, height=200)
    page.insert_image(page.rect, stream=image)
    ocr = FakeOcr()
    result = PdfExtractor(ocr).extract_page(page)
    assert result.status == "blank" and ocr.calls == 0
    document.close()
    source.close()


def test_ocr_coordinates_use_unrotated_page_space():
    doc = make_document()
    page = doc[1]
    page.set_rotation(90)
    result = PdfExtractor(FakeOcr()).extract_page(page)
    assert result.status == "completed"
    x0, y0, x1, y1 = result.blocks[0].bbox
    assert 0 <= x0 < x1 <= page.cropbox.width
    assert 0 <= y0 < y1 <= page.cropbox.height
    doc.close()
