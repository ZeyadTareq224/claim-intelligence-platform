"""Page-oriented PDF extraction; persistence and input validation are separate."""
from dataclasses import dataclass, field
import csv
import io
import math
import re
import subprocess  # nosec B404: Tesseract runs without a shell.
from pathlib import Path

import pymupdf


@dataclass(frozen=True)
class TextBlock:
    text: str
    method: str
    bbox: tuple[float, float, float, float] | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class PageResult:
    page_number: int
    status: str
    method: str | None
    width: float
    height: float
    rotation: int
    blocks: list[TextBlock] = field(default_factory=list)
    reason: str | None = None
    error: str | None = None


class OcrError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TesseractOcr:
    def __init__(self, language: str = "eng", timeout: float = 60):
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", language) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid OCR language or timeout")
        self.language = language
        self.timeout = timeout

    def recognize(self, image: bytes) -> list[tuple[str, tuple[int, int, int, int], float]]:
        try:
            result = subprocess.run(  # nosec B603,B607: Fixed executable, validated language, no shell.
                ["tesseract", "stdin", "stdout", "-l", self.language, "tsv"],
                input=image, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout, check=True,
            )
        except FileNotFoundError as exc:
            raise OcrError("ocr_unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise OcrError("ocr_timeout") from exc
        except (OSError, subprocess.CalledProcessError) as exc:
            raise OcrError("ocr_failed") from exc
        try:
            rows = csv.DictReader(io.StringIO(result.stdout.decode("utf-8")), delimiter="\t")
            words = []
            for row in rows:
                text = row["text"].strip()
                if text:
                    left, top, width, height = (int(row[key]) for key in ("left", "top", "width", "height"))
                    words.append((text, (left, top, left + width, top + height), float(row["conf"])))
            return words
        except (UnicodeError, KeyError, TypeError, ValueError, csv.Error) as exc:
            raise OcrError("ocr_invalid_output") from exc


def usable_text(text: str) -> bool:
    visible = [c for c in text if not c.isspace()]
    if not visible:
        return False
    valid = sum(c.isalnum() for c in visible)
    corrupt = sum(c == "\ufffd" or ord(c) < 32 or 0xE000 <= ord(c) <= 0xF8FF for c in visible)
    return valid >= 2 and valid / len(visible) >= 0.4 and corrupt / len(visible) < 0.1


class PdfExtractor:
    def __init__(self, ocr: TesseractOcr | None = None, dpi: int = 200, max_pixels: int = 25_000_000):
        if not 72 <= dpi <= 300 or max_pixels <= 0:
            raise ValueError("invalid OCR resolution or pixel limit")
        self.ocr = ocr or TesseractOcr()
        self.dpi = dpi
        self.max_pixels = max_pixels

    def extract(self, path: Path) -> list[PageResult]:
        with pymupdf.open(path) as pdf:
            return [self.extract_page(page) for page in pdf]

    def extract_bytes(self, data: bytes) -> list[PageResult]:
        return list(self.iter_bytes(data))

    def iter_bytes(self, data: bytes, page_numbers: set[int] | None = None):
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            for index in range(pdf.page_count):
                if page_numbers is not None and index + 1 not in page_numbers:
                    continue
                try:
                    page = pdf.load_page(index)
                except (RuntimeError, ValueError):
                    yield PageResult(index + 1, "failed", None, 0, 0, 0, error="page_load_failed")
                    continue
                yield self.extract_page(page)

    def extract_page(self, page) -> PageResult:
        number = page.number + 1
        # PyMuPDF native text coordinates are in unrotated points.
        width, height = page.cropbox.width, page.cropbox.height
        base = dict(page_number=number, width=width, height=height, rotation=page.rotation)
        native = []
        reason = None
        try:
            native = [TextBlock(block[4], "native", tuple(block[:4]))
                      for block in page.get_text("blocks", sort=True) if block[6] == 0 and block[4].strip()]
            text = " ".join(block.text for block in native)
            if usable_text(text):
                # Detect separate image regions, but skip images overlapping an existing text layer.
                regions = []
                for info in page.get_image_info():
                    rect = pymupdf.Rect(info["bbox"]) & page.cropbox
                    if rect.is_empty or rect.width < 25 or rect.height < 25:
                        continue
                    if any(not (pymupdf.Rect(block.bbox) & rect).is_empty for block in native):
                        continue
                    regions.append(rect)
                if not regions:
                    return PageResult(**base, status="completed", method="native", blocks=native)
                reason = "image_text_region"
                blocks = list(native)
                for region in regions:
                    blocks.extend(self._ocr_region(page, region) or [])
                blocks.sort(key=lambda block: (block.bbox[1], block.bbox[0]))
                method = "mixed" if len(blocks) > len(native) else "native"
                return PageResult(**base, status="completed", method=method, blocks=blocks,
                                  reason="image_text_region" if method == "mixed" else None)
            if not native and not page.get_image_info() and not page.get_drawings():
                return PageResult(**base, status="blank", method=None)
            reason = "unusable_native_text" if native else "no_native_text"
            blocks = self._ocr_region(page, None)
            if blocks is None:
                return PageResult(**base, status="blank", method=None)
            if blocks:
                return PageResult(**base, status="completed", method="ocr", blocks=blocks, reason=reason)
            return PageResult(**base, status="failed", method="ocr", reason=reason, error="ocr_no_text")
        except OcrError as exc:
            retained = native if usable_text(" ".join(block.text for block in native)) else []
            return PageResult(**base, status="failed", method="mixed" if retained else "ocr",
                              blocks=retained, reason=reason, error=exc.code)
        except (RuntimeError, ValueError):
            return PageResult(**base, status="failed", method=None, error="page_extraction_failed")

    def _ocr_region(self, page, region):
        clip = page.rect if region is None else (region * page.rotation_matrix) & page.rect
        scale = self.dpi / 72
        if clip.width * scale * clip.height * scale > self.max_pixels:
            raise OcrError("ocr_image_too_large")
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, colorspace=pymupdf.csGRAY, alpha=False)
        if min(pix.samples, default=255) >= 250:
            return None
        words = self.ocr.recognize(pix.tobytes("png"))
        blocks = []
        for text, bbox, confidence in words:
            rect = pymupdf.Rect(bbox[0] / scale + clip.x0, bbox[1] / scale + clip.y0,
                                bbox[2] / scale + clip.x0, bbox[3] / scale + clip.y0)
            rect = rect * page.derotation_matrix
            blocks.append(TextBlock(text, "ocr", tuple(rect), confidence))
        return blocks
