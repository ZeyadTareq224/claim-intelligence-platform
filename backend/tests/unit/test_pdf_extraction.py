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
    page = PdfExtractor(ocr)._extract_page(doc[0])
    assert page.method == "mixed" and [block.method for block in page.blocks] == ["native", "ocr"]
    assert ocr.calls == 1
    doc.close()


def test_ocr_failure_keeps_other_pages_and_native_text():
    doc = make_document()
    doc[0].insert_image(pymupdf.Rect(50, 150, 350, 250), stream=doc[1].get_pixmap().tobytes("png"))
    extractor = PdfExtractor(FakeOcr("ocr_timeout"))
    first, second, third = [extractor._extract_page(page) for page in doc]
    assert first.status == "failed" and first.error == "ocr_timeout" and first.blocks[0].method == "native"
    assert second.status == "failed" and second.error == "ocr_timeout"
    assert third.status == "blank"
    doc.close()


def test_existing_text_layer_prevents_ocr():
    doc = make_document()
    doc[1].insert_text((40, 80), "Scanned page", fontsize=20)
    ocr = FakeOcr()
    page = PdfExtractor(ocr)._extract_page(doc[1])
    assert page.method == "native" and ocr.calls == 0
    doc.close()


def test_rejects_unusable_text_and_bounds_rendering():
    assert not usable_text("\ufffd\ufffd\ufffd")
    assert usable_text("P-123")
    doc = make_document()
    result = PdfExtractor(FakeOcr(), max_pixels=1)._extract_page(doc[1])
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
    result = PdfExtractor(ocr)._extract_page(page)
    assert result.status == "blank" and ocr.calls == 0
    document.close()
    source.close()


def test_ocr_coordinates_use_unrotated_page_space():
    doc = make_document()
    page = doc[1]
    page.set_rotation(90)
    result = PdfExtractor(FakeOcr())._extract_page(page)
    assert result.status == "completed"
    x0, y0, x1, y1 = result.blocks[0].bbox
    assert 0 <= x0 < x1 <= page.cropbox.width
    assert 0 <= y0 < y1 <= page.cropbox.height
    doc.close()


def test_iter_bytes_skips_pages_not_requested():
    doc = make_document()
    data = doc.tobytes()
    doc.close()
    ocr = FakeOcr()
    pages = list(PdfExtractor(ocr).iter_bytes(data, {1, 3}))
    assert [page.page_number for page in pages] == [1, 3]
    assert ocr.calls == 0


def test_ocr_timeout_must_be_finite():
    with pytest.raises(ValueError):
        TesseractOcr(timeout=float("nan"))


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_native_timeout_must_be_positive_and_finite(value):
    with pytest.raises(ValueError, match="native extraction timeout"):
        PdfExtractor(native_timeout=value)


def test_native_timeout_stops_worker_and_later_page_succeeds(monkeypatch):
    import sys
    import time

    doc = make_document()
    data = doc.tobytes()
    doc.close()
    extractor = PdfExtractor(FakeOcr(), native_timeout=1.5)
    start_worker = extractor._start_worker
    stopped = []

    def worker(pdf_path, image_path, number):
        if number == 1:
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            stopped.append(process)
            return process
        return start_worker(pdf_path, image_path, number)

    monkeypatch.setattr(extractor, "_start_worker", worker)
    start = time.monotonic()
    pages = list(extractor.iter_bytes(data, {1, 3}))
    assert time.monotonic() - start < 5
    assert pages[0].status == "failed" and pages[0].error == "native_timeout"
    assert pages[1].status == "blank"
    assert stopped[0].poll() is not None


def test_ocr_time_does_not_count_against_native_limit():
    import time

    class SlowOcr(FakeOcr):
        def recognize(self, image):
            time.sleep(1.2)
            return super().recognize(image)

    doc = make_document()
    data = doc.tobytes()
    doc.close()
    page = list(PdfExtractor(SlowOcr(), native_timeout=1).iter_bytes(data, {2}))[0]
    assert page.status == "completed" and page.method == "ocr"


def test_ingestion_reads_native_timeout_from_environment(monkeypatch):
    from app.services.document_ingestion import DocumentIngestionService

    monkeypatch.setenv("PDF_NATIVE_TIMEOUT_SECONDS", "4.5")
    service = DocumentIngestionService.from_environment(None)
    assert service.extractor.native_timeout == 4.5
    assert service.inputs.inspector.native_timeout == 4.5


def test_native_timeout_also_bounds_writing_ocr_reply(monkeypatch):
    import sys
    import time

    class LargeOcr:
        def recognize(self, image):
            return [("A" * 200, (1, 1, 2, 2), 90)] * 10000

    doc = make_document()
    data = doc.tobytes()
    doc.close()
    extractor = PdfExtractor(LargeOcr(), native_timeout=1.5)
    processes = []

    def stalled(pdf_path, image_path, number):
        code = (
            "from pathlib import Path; import sys, time; "
            "Path(sys.argv[1]).write_bytes(b'png'); "
            "print('{\\\"event\\\":\\\"ocr\\\"}', flush=True); time.sleep(10)"
        )
        process = subprocess.Popen([sys.executable, "-c", code, str(image_path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        processes.append(process)
        return process

    monkeypatch.setattr(extractor, "_start_worker", stalled)
    started = time.monotonic()
    page = list(extractor.iter_bytes(data, {1}))[0]
    assert time.monotonic() - started < 5
    assert page.error == "native_timeout" and processes[0].poll() is not None


@pytest.mark.skipif(not __import__("sys").platform.startswith("linux"), reason="Linux parent-death signal")
def test_page_worker_exits_when_parent_is_killed(tmp_path):
    import os
    import select
    import sys
    import time

    doc = make_document()
    path = tmp_path / "test.pdf"
    path.write_bytes(doc.tobytes())
    doc.close()
    image_path = tmp_path / "ocr.png"
    code = (
        "from pathlib import Path; import sys, time; "
        "from app.services.pdf_extraction import PdfExtractor; "
        "p=PdfExtractor()._start_worker(Path(sys.argv[1]), Path(sys.argv[2]), 2); "
        "assert b'\"event\": \"ocr\"' in p.stdout.readline(); print(p.pid, flush=True); time.sleep(20)"
    )
    parent = subprocess.Popen([sys.executable, "-c", code, str(path), str(image_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([parent.stdout], [], [], 5)[0], "worker did not request OCR"
        child_pid = int(parent.stdout.readline().strip())
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                state = __import__("pathlib").Path(f"/proc/{child_pid}/stat").read_text(encoding="utf-8").split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.05)
        else:
            os.kill(child_pid, 9)
            pytest.fail("worker survived its parent")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        parent.stdout.close()
        parent.stderr.close()


def test_tesseract_version_is_recorded_without_raw_engine_output():
    from types import SimpleNamespace

    with patch("app.services.pdf_extraction.subprocess.run", return_value=SimpleNamespace(stdout=b"tesseract 5.5.0\nextra details")):
        assert TesseractOcr().engine_version() == "5.5.0"
    with patch("app.services.pdf_extraction.subprocess.run", side_effect=FileNotFoundError()):
        assert TesseractOcr().engine_version() == "unavailable"


def test_scanned_page_with_small_selectable_header_ocr_recovers_body_without_duplicate():
    source = pymupdf.open()
    scanned = source.new_page(width=612, height=792)
    scanned.insert_text((72, 90), "POLICY NUMBER P-4004", fontsize=22)
    scanned.insert_text((72, 190), "SUM INSURED EGP 32000", fontsize=22)
    image = scanned.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=image)
    page.insert_text((72, 90), "POLICY NUMBER P-4004", fontsize=22)

    class HybridOcr:
        calls = 0

        def recognize(self, image):
            self.calls += 1
            scale = 200 / 72
            def box(x0, y0, x1, y1):
                return tuple(round(value * scale) for value in (x0, y0, x1, y1))
            return [("POLICY", box(72, 72, 155, 92), 95),
                    ("SUM", box(72, 172, 120, 192), 95),
                    ("32000", box(250, 172, 330, 192), 95)]

    ocr = HybridOcr()
    result = PdfExtractor(ocr)._extract_page(page)
    assert result.status == "completed" and result.method == "mixed" and ocr.calls == 1
    assert sum("POLICY" in block.text for block in result.blocks) == 1
    assert any(block.text == "32000" and block.method == "ocr" for block in result.blocks)
    document.close()
    source.close()


def test_rotated_scanned_page_with_selectable_header_still_uses_ocr():
    source = pymupdf.open()
    scanned = source.new_page(width=612, height=792)
    scanned.insert_text((72, 90), "POLICY NUMBER P-4004", fontsize=22)
    scanned.insert_text((72, 190), "SUM INSURED EGP 32000", fontsize=22)
    image = scanned.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=image)
    page.insert_text((72, 90), "POLICY NUMBER P-4004", fontsize=22)
    page.set_rotation(90)
    ocr = FakeOcr()
    result = PdfExtractor(ocr)._extract_page(page)
    assert result.status == "completed" and result.method == "mixed" and ocr.calls == 1
    document.close()
    source.close()


def test_complete_selectable_layer_does_not_trigger_ocr():
    source = pymupdf.open()
    scanned = source.new_page(width=612, height=792)
    scanned.insert_text((72, 90), "POLICY NUMBER P-4004", fontsize=22)
    scanned.insert_text((72, 190), "SUM INSURED EGP 32000", fontsize=22)
    image = scanned.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=image)
    page.insert_text((72, 90), "POLICY NUMBER P-4004", fontsize=22)
    page.insert_text((72, 190), "SUM INSURED EGP 32000", fontsize=22)
    ocr = FakeOcr()
    result = PdfExtractor(ocr)._extract_page(page)
    assert result.status == "completed" and result.method == "native" and ocr.calls == 0
    document.close()
    source.close()


def test_ocr_deduplication_keeps_same_word_at_a_different_location():
    from app.services.pdf_extraction import TextBlock

    native = TextBlock("POLICY NUMBER", "native", (10, 10, 200, 30))
    duplicate = TextBlock("Policy", "ocr", (10, 10, 80, 30))
    repeated = TextBlock("Policy", "ocr", (10, 100, 80, 120))
    assert PdfExtractor._duplicate_block(duplicate, [native])
    assert not PdfExtractor._duplicate_block(repeated, [native])


def test_hybrid_ocr_failure_keeps_selectable_header():
    source = pymupdf.open()
    scanned = source.new_page(width=612, height=792)
    scanned.insert_text((72, 90), "POLICY NUMBER P-4004", fontsize=22)
    scanned.insert_text((72, 190), "SUM INSURED EGP 32000", fontsize=22)
    image = scanned.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=image)
    page.insert_text((72, 90), "POLICY NUMBER P-4004", fontsize=22)
    result = PdfExtractor(FakeOcr("ocr_timeout"))._extract_page(page)
    assert result.status == "failed" and result.error == "ocr_timeout"
    assert result.method == "mixed" and "P-4004" in result.blocks[0].text
    document.close()
    source.close()
