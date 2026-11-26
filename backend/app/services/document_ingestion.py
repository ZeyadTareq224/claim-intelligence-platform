"""Connect validated PDFs to page extraction and transactional evidence storage."""
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pymupdf

from app.repositories.documents import DocumentRepository
from app.services.pdf_extraction import PdfExtractor
from app.services.pdf_input import PdfInputError, PdfInputService


EXTRACTOR_VERSION = "1"


class DocumentIngestionService:
    def __init__(self, inputs: PdfInputService, extractor: PdfExtractor, repository: DocumentRepository):
        self.inputs = inputs
        self.extractor = extractor
        self.repository = repository

    def ingest(self, source_type: str, document_id: UUID, path: Path):
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
            "ocr_timeout": self.extractor.ocr.timeout,
            "ocr_dpi": self.extractor.dpi,
            "max_ocr_pixels": self.extractor.max_pixels,
        }
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        run_id = self.repository.create(source, config, hashlib.sha256(encoded).hexdigest(), EXTRACTOR_VERSION)
        self.repository.start(run_id)
        stage = "extraction_failed"
        try:
            for expected, page in enumerate(self.extractor.iter_bytes(data), start=1):
                if page.page_number != expected or expected > source.page_count:
                    raise PdfInputError("page_count_mismatch")
                stage = "persistence_failed"
                self.repository.save_page(run_id, page)
                stage = "extraction_failed"
        except Exception as exc:
            code = exc.code if isinstance(exc, PdfInputError) else stage
            self.repository.finish(run_id, code)
            raise
        self.repository.finish(run_id)
        return run_id
