"""Page-oriented PDF extraction; persistence and input validation are separate."""
from dataclasses import dataclass, field
import json
import os
import select
import sys
import tempfile
import time
import csv
import io
import math
import re
import subprocess  # nosec
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

    def engine_version(self) -> str:
        try:
            # A fixed command records the OCR engine version without invoking a shell.
            result = subprocess.run(  # nosec
                ["tesseract", "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=5, check=True,
            )
            line = result.stdout.decode("utf-8", errors="replace").splitlines()[0]
            match = re.match(r"tesseract\s+([A-Za-z0-9._+~-]{1,80})(?:\s|$)", line, re.IGNORECASE)
            return match.group(1) if match else "unknown"
        except (OSError, subprocess.SubprocessError, IndexError):
            return "unavailable"

    def recognize(self, image: bytes) -> list[tuple[str, tuple[int, int, int, int], float]]:
        try:
            # The executable is fixed, language is validated, and no shell is used.
            result = subprocess.run(  # nosec
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
    def __init__(self, ocr: TesseractOcr | None = None, dpi: int = 200, max_pixels: int = 25_000_000,
                 native_timeout: float = 30):
        if not 72 <= dpi <= 300 or max_pixels <= 0:
            raise ValueError("invalid OCR resolution or pixel limit")
        if not math.isfinite(native_timeout) or native_timeout <= 0:
            raise ValueError("invalid native extraction timeout")
        self.ocr = ocr or TesseractOcr()
        self.dpi = dpi
        self.max_pixels = max_pixels
        self.native_timeout = native_timeout

    def extract(self, path: Path) -> list[PageResult]:
        return self.extract_bytes(path.read_bytes())

    def extract_bytes(self, data: bytes) -> list[PageResult]:
        return list(self.iter_bytes(data))

    def inspect_bytes(self, data: bytes) -> dict:
        with tempfile.TemporaryDirectory(prefix="pdf-inspect-") as directory:
            pdf_path = Path(directory) / "source.pdf"
            pdf_path.write_bytes(data)
            return self._run_worker(pdf_path, Path(directory) / "ocr.png", 0)

    def iter_bytes(self, data: bytes, page_numbers: set[int] | None = None):
        with tempfile.TemporaryDirectory(prefix="pdf-extract-") as directory:
            pdf_path = Path(directory) / "source.pdf"
            image_path = Path(directory) / "ocr.png"
            pdf_path.write_bytes(data)
            if page_numbers is None:
                info = self._run_worker(pdf_path, image_path, 0)
                if "page_count" not in info:
                    raise RuntimeError(info.get("error", "page_extraction_failed"))
                page_numbers = set(range(1, info["page_count"] + 1))
            for number in sorted(page_numbers):
                result = self._run_worker(pdf_path, image_path, number)
                if "page_number" not in result:
                    yield PageResult(number, "failed", None, 0, 0, 0, error=result.get("error", "native_worker_failed"))
                else:
                    blocks = [TextBlock(**{**block, "bbox": tuple(block["bbox"]) if block["bbox"] is not None else None})
                              for block in result.pop("blocks")]
                    yield PageResult(**result, blocks=blocks)

    def _start_worker(self, pdf_path: Path, image_path: Path, number: int):
        # The module and temporary paths are controlled, and no shell is used.
        return subprocess.Popen(  # nosec
            [sys.executable, "-m", "app.services.pdf_page_worker", str(pdf_path), str(image_path),
             str(number), str(self.dpi), str(self.max_pixels)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def _run_worker(self, pdf_path: Path, image_path: Path, number: int) -> dict:
        try:
            worker = self._start_worker(pdf_path, image_path, number)
        except OSError:
            return {"error": "native_worker_failed"}
        deadline = time.monotonic() + self.native_timeout
        buffer = b""
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([worker.stdout], [], [], remaining)[0]:
                    return {"error": "native_timeout"}
                chunk = os.read(worker.stdout.fileno(), 65536)
                if not chunk:
                    return {"error": "native_worker_failed"}
                buffer += chunk
                if len(buffer) > 32 * 1024 * 1024:
                    return {"error": "native_output_too_large"}
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    message = json.loads(line)
                    if message["event"] == "ocr":
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return {"error": "native_timeout"}
                        try:
                            with image_path.open("rb") as image:
                                payload = image.read(self.max_pixels * 4 + 1)
                            if len(payload) > self.max_pixels * 4:
                                raise OcrError("ocr_image_too_large")
                            reply = {"words": self.ocr.recognize(payload)}
                        except OcrError as exc:
                            reply = {"error": exc.code}
                        except OSError:
                            reply = {"error": "ocr_failed"}
                        deadline = time.monotonic() + remaining
                        response = (json.dumps(reply) + "\n").encode()
                        if len(response) > 32 * 1024 * 1024:
                            response = b'{"error":"ocr_output_too_large"}\n'
                        fd = worker.stdin.fileno()
                        os.set_blocking(fd, False)
                        offset = 0
                        while offset < len(response):
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                                return {"error": "native_timeout"}
                            offset += os.write(fd, response[offset:offset + 65536])
                    elif message["event"] == "result":
                        return message["value"]
                    else:
                        return {"error": "native_worker_failed"}
        except (OSError, ValueError, KeyError, TypeError):
            return {"error": "native_worker_failed"}
        finally:
            if worker.poll() is None:
                try:
                    worker.kill()
                except ProcessLookupError:
                    pass
            worker.wait()
            if worker.stdin:
                worker.stdin.close()
            if worker.stdout:
                worker.stdout.close()

    def _extract_page(self, page) -> PageResult:
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
