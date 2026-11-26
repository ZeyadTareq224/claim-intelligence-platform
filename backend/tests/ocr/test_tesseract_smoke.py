"""Real OCR smoke test; Tesseract and its English data are required."""
import pymupdf

from app.services.pdf_extraction import PdfExtractor


def test_real_tesseract_recognizes_scanned_page():
    source = pymupdf.open()
    page = source.new_page(width=500, height=160)
    page.insert_text((30, 60), "POLICY NUMBER ABC123", fontsize=26)
    image = page.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    pdf = pymupdf.open()
    scan = pdf.new_page(width=500, height=160)
    scan.insert_image(scan.rect, stream=image)
    result = PdfExtractor().extract_page(scan)
    assert result.status == "completed", result.error
    assert result.method == "ocr"
    assert "ABC123" in " ".join(block.text for block in result.blocks)
    assert all(block.bbox and block.confidence is not None for block in result.blocks)
    pdf.close()
    source.close()
