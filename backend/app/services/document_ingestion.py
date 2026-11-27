"""Connect validated PDFs to page extraction and transactional evidence storage."""
from collections import Counter
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from uuid import UUID

import pymupdf

from app.repositories.documents import DocumentRepository
from app.services.pdf_extraction import PdfExtractor, TesseractOcr
from app.services.pdf_input import PdfInputError, PdfInputService


EXTRACTOR_VERSION = "2"
logger = logging.getLogger(__name__)


class DocumentIngestionService:
    def __init__(self, inputs: PdfInputService, extractor: PdfExtractor, repository: DocumentRepository):
        self.inputs = inputs
        self.extractor = extractor
        self.repository = repository

    @classmethod
    def from_environment(cls, sessions):
        ocr = TesseractOcr(
            language=os.environ.get("PDF_OCR_LANGUAGE", "eng"),
            timeout=float(os.environ.get("PDF_OCR_TIMEOUT_SECONDS", "60")),
        )
        extractor = PdfExtractor(ocr=ocr, dpi=int(os.environ.get("PDF_OCR_DPI", "200")),
                                 max_pixels=int(os.environ.get("PDF_OCR_MAX_PIXELS", "25000000")),
                                 native_timeout=float(os.environ.get("PDF_NATIVE_TIMEOUT_SECONDS", "30")))
        return cls(PdfInputService.from_environment(sessions), extractor, DocumentRepository(sessions))

    def ingest(self, source_type: str, document_id: UUID, path: Path):
        started = time.monotonic()
        try:
            return self._ingest(source_type, document_id, path, started)
        except PdfInputError as exc:
            logger.warning("document_processing_failed source_type=%s document_id=%s error_code=%s duration_ms=%d",
                           source_type, document_id, exc.code, (time.monotonic() - started) * 1000)
            raise
        except Exception:
            logger.error("document_processing_failed source_type=%s document_id=%s error_code=unexpected duration_ms=%d",
                         source_type, document_id, (time.monotonic() - started) * 1000)
            raise

    def _ingest(self, source_type: str, document_id: UUID, path: Path, started: float):
        source = self.inputs.validate(source_type, document_id, path)
        stored = self.inputs.storage_dir / f"{source.sha256}.pdf"
        try:
            with stored.open("rb") as preserved:
                data = preserved.read(self.inputs.max_bytes + 1)
        except OSError as exc:
            raise PdfInputError("storage_error") from exc
        if len(data) > self.inputs.max_bytes or hashlib.sha256(data).hexdigest() != source.sha256:
            raise PdfInputError("storage_conflict")
        config = {
            "extractor_version": EXTRACTOR_VERSION,
            "pymupdf_version": pymupdf.VersionBind,
            "ocr_language": self.extractor.ocr.language,
            "tesseract_version": self.extractor.ocr.engine_version(),
            "ocr_timeout": self.extractor.ocr.timeout,
            "ocr_dpi": self.extractor.dpi,
            "max_ocr_pixels": self.extractor.max_pixels,
            "native_timeout": self.extractor.native_timeout,
        }
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        config_hash = hashlib.sha256(encoded).hexdigest()
        with self.repository.claim(source, config_hash) as repository:
            existing = repository.find(source, config_hash)
            if existing is None:
                run_id = repository.create(source, config, config_hash, EXTRACTOR_VERSION)
                completed = set()
            else:
                run_id, status, completed = existing
                if status == "completed":
                    logger.info("document_processing_reused source_type=%s document_id=%s run_id=%s status=%s page_count=%d duration_ms=%d",
                                source_type, document_id, run_id, status, source.page_count, (time.monotonic() - started) * 1000)
                    return run_id
            remaining = set(range(1, source.page_count + 1)) - completed
            repository.start(run_id)
            stage = "extraction_failed"
            methods = Counter()
            errors = Counter()
            page_failures = 0
            try:
                expected = iter(sorted(remaining))
                for page in self.extractor.iter_bytes(data, remaining):
                    if page.page_number != next(expected, None):
                        raise PdfInputError("page_count_mismatch")
                    stage = "persistence_failed"
                    repository.save_page(run_id, page)
                    methods[page.method or "none"] += 1
                    page_failures += int(page.status == "failed")
                    if page.error:
                        errors[page.error] += 1
                    stage = "extraction_failed"
                missing = next(expected, None) is not None
            except Exception as exc:
                code = exc.code if isinstance(exc, PdfInputError) else stage
                status = repository.finish(run_id, code)
                logger.warning("document_run_failed source_type=%s document_id=%s run_id=%s status=%s error_code=%s page_count=%d processed_pages=%d duration_ms=%d",
                               source_type, document_id, run_id, status, code, source.page_count, sum(methods.values()),
                               (time.monotonic() - started) * 1000)
                raise
            status = repository.finish(run_id, "page_count_mismatch" if missing else None)
            log = logger.info if status == "completed" else logger.warning
            log("document_processed source_type=%s document_id=%s run_id=%s status=%s page_count=%d processed_pages=%d reused_pages=%d methods=%s failed_pages=%d error_codes=%s duration_ms=%d",
                source_type, document_id, run_id, status, source.page_count, sum(methods.values()),
                len(completed), dict(methods), page_failures, dict(errors), (time.monotonic() - started) * 1000)
            return run_id
