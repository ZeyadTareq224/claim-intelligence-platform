"""Generate fictional English PDF regression fixtures; no customer data."""
from pathlib import Path
import pymupdf

ROOT = Path(__file__).resolve().parent


def text_page(document, lines):
    page = document.new_page(width=612, height=792)
    for index, line in enumerate(lines):
        page.insert_text((72, 90 + 32 * index), line, fontsize=22, fontname="helv")
    return page


def scanned_page(document, lines):
    drawing = pymupdf.open()
    page = text_page(drawing, lines)
    image = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), colorspace=pymupdf.csGRAY).tobytes("png")
    target = document.new_page(width=612, height=792)
    target.insert_image(target.rect, stream=image)
    drawing.close()


def create(name, pages):
    doc = pymupdf.open()
    for kind, lines in pages:
        if kind == "native":
            text_page(doc, lines)
        elif kind == "ocr":
            scanned_page(doc, lines)
        elif kind == "hybrid":
            scanned_page(doc, lines)
            doc[-1].insert_text((72, 90), lines[0], fontsize=22, fontname="helv")
        else:
            doc.new_page(width=612, height=792)
    doc.save(ROOT / name, garbage=4, deflate=True, no_new_id=True)
    doc.close()


def main():
    create("digital-policy.pdf", [("native", ["POLICY NUMBER P-1001", "SUM INSURED EGP 12000"])])
    create("scanned-policy.pdf", [("ocr", ["POLICY NUMBER P-2002", "SUM INSURED EGP 25000"])])
    create("mixed-policy.pdf", [("native", ["POLICY NUMBER P-3003", "SUM INSURED EGP 18000"]),
                                ("ocr", ["POLICY NUMBER P-3003", "SUM INSURED EGP 18000"])])
    create("hybrid-policy.pdf", [("hybrid", ["POLICY NUMBER P-4004", "SUM INSURED EGP 32000"])])
    create("blank.pdf", [("blank", [])])
    (ROOT / "malformed.pdf").write_bytes(b"%PDF-1.7\nThis intentionally malformed PDF has no document structure.\n")


if __name__ == "__main__":
    main()
