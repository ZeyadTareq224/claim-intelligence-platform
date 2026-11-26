"""Isolated PyMuPDF page worker; OCR requests are handled by the parent."""
from dataclasses import asdict
import ctypes
import json
import os
from pathlib import Path
import signal
import sys

import pymupdf

from app.services.pdf_extraction import OcrError, PdfExtractor


def send(event, **payload):
    print(json.dumps({"event": event, **payload}, ensure_ascii=True), flush=True)


class ParentOcr:
    def __init__(self, image_path):
        self.image_path = image_path

    def recognize(self, image):
        self.image_path.write_bytes(image)
        send("ocr")
        reply = json.loads(sys.stdin.buffer.readline())
        if "error" in reply:
            raise OcrError(reply["error"])
        return reply["words"]


def main():
    if sys.platform.startswith("linux"):
        parent = os.getppid()
        # Avoid leaving a stuck native worker behind if the calling process dies.
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "could not set parent-death signal")
        if os.getppid() != parent:
            os._exit(1)
    pdf_path, image_path, number, dpi, max_pixels = sys.argv[1:]
    try:
        with pymupdf.open(pdf_path) as pdf:
            if pdf.needs_pass or pdf.is_encrypted or pdf.metadata.get("encryption"):
                send("result", value={"error": "encrypted_pdf"})
                return
            if pdf.is_repaired or pdf.page_count == 0:
                send("result", value={"error": "invalid_pdf"})
                return
            if int(number) == 0:
                send("result", value={"page_count": pdf.page_count})
                return
            try:
                page = pdf.load_page(int(number) - 1)
            except (RuntimeError, ValueError):
                send("result", value={"error": "page_load_failed"})
                return
            extractor = PdfExtractor(ocr=ParentOcr(Path(image_path)), dpi=int(dpi), max_pixels=int(max_pixels))
            send("result", value=asdict(extractor._extract_page(page)))
    except (RuntimeError, ValueError):
        send("result", value={"error": "invalid_pdf"})


if __name__ == "__main__":
    main()
