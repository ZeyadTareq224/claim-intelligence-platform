"""Transactional storage for extracted document evidence."""
from contextlib import contextmanager
import hashlib
import json

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload, sessionmaker

from app.domain.models import ClaimDocument, PolicyDocument, DocumentProcessingRun, ExtractedPage, ExtractedBlock
from app.services.pdf_input import PdfInputError


class DocumentRepository:
    def __init__(self, sessions, claim_connection=None):
        self.sessions = sessions
        self.claim_connection = claim_connection

    def _check_claim(self):
        if self.claim_connection is not None and self.claim_connection.invalidated:
            raise PdfInputError("processing_claim_lost")

    @contextmanager
    def claim(self, source, config_hash):
        identity = [source.source_type, str(source.document_id), source.policy_document_version,
                    source.sha256, config_hash]
        digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).digest()
        key = int.from_bytes(digest[:8], "big", signed=True)
        with self.sessions() as session:
            bind = session.get_bind()
        engine = bind.engine if hasattr(bind, "engine") else bind
        with engine.connect() as connection:
            acquired = None
            try:
                acquired = connection.scalar(select(func.pg_try_advisory_lock(key)))
                connection.commit()
                if not acquired:
                    raise PdfInputError("processing_in_progress")
                yield DocumentRepository(sessionmaker(bind=connection, expire_on_commit=False), connection)
            finally:
                if acquired is None:
                    connection.invalidate()
                elif acquired and not connection.invalidated:
                    try:
                        connection.rollback()
                        released = connection.scalar(select(func.pg_advisory_unlock(key)))
                        connection.commit()
                        if not released:
                            raise PdfInputError("processing_claim_lost")
                    except BaseException:
                        connection.invalidate()
                        raise

    def find(self, source, config_hash):
        self._check_claim()
        source_column = (DocumentProcessingRun.policy_document_id if source.source_type == "policy"
                         else DocumentProcessingRun.claim_document_id)
        with self.sessions() as session:
            statement = select(DocumentProcessingRun).where(
                source_column == source.document_id,
                DocumentProcessingRun.source_hash == source.sha256,
                DocumentProcessingRun.config_hash == config_hash,
            ).options(selectinload(DocumentProcessingRun.pages))
            if source.source_type == "policy":
                statement = statement.where(DocumentProcessingRun.policy_document_version == source.policy_document_version)
            run = session.scalar(statement)
            if run is None:
                return None
            return run.id, run.status, {page.page_number for page in run.pages if page.status in ("completed", "blank")}

    def create(self, source, config, config_hash, version):
        self._check_claim()
        with self.sessions.begin() as session:
            model = PolicyDocument if source.source_type == "policy" else ClaimDocument
            document = session.get(model, source.document_id, with_for_update=True)
            if document is None:
                raise PdfInputError("document_not_found")
            if source.source_type == "policy" and document.document_version != source.policy_document_version:
                raise PdfInputError("document_version_changed")
            run = DocumentProcessingRun(
                policy_document_id=source.document_id if source.source_type == "policy" else None,
                claim_document_id=source.document_id if source.source_type == "claim" else None,
                policy_document_version=source.policy_document_version,
                source_hash=source.sha256, extractor_version=version, config_hash=config_hash,
                config=config, status="pending", page_count=source.page_count,
            )
            session.add(run)
            session.flush()
            return run.id

    def start(self, run_id):
        self._check_claim()
        with self.sessions.begin() as session:
            result = session.execute(update(DocumentProcessingRun).where(
                DocumentProcessingRun.id == run_id, DocumentProcessingRun.status.in_(("pending", "processing", "partial", "failed"))
            ).values(status="processing", started_at=func.now(), finished_at=None, error_code=None))
            if result.rowcount != 1:
                raise PdfInputError("run_not_retryable")

    def save_page(self, run_id, page):
        self._check_claim()
        with self.sessions.begin() as session:
            existing = session.scalar(select(ExtractedPage).where(
                ExtractedPage.run_id == run_id, ExtractedPage.page_number == page.page_number
            ).options(selectinload(ExtractedPage.blocks)))
            if existing is not None:
                if existing.status != "failed":
                    raise PdfInputError("page_already_completed")
                if existing.blocks and page.status == "failed" and not page.blocks:
                    existing.error, existing.reason = page.error, page.reason
                    return
                session.delete(existing)
                session.flush()
            session.add(ExtractedPage(
                run_id=run_id, page_number=page.page_number, status=page.status, method=page.method,
                reason=page.reason, error=page.error, width=page.width, height=page.height,
                rotation=page.rotation,
                blocks=[ExtractedBlock(block_index=index, raw_text=block.text,
                    normalized_text=" ".join(block.text.split()), block_type="unknown", method=block.method,
                    bbox=list(block.bbox) if block.bbox is not None else None, confidence=block.confidence)
                    for index, block in enumerate(page.blocks)],
            ))

    def finish(self, run_id, error_code=None):
        self._check_claim()
        with self.sessions.begin() as session:
            run = session.scalar(select(DocumentProcessingRun).where(DocumentProcessingRun.id == run_id)
                                 .options(selectinload(DocumentProcessingRun.pages).selectinload(ExtractedPage.blocks)))
            if run is None or run.status != "processing":
                raise PdfInputError("run_not_processing")
            pages = run.pages
            if error_code is None and len(pages) != run.page_count:
                error_code = "page_count_mismatch"
            failed = error_code is not None or any(page.status == "failed" for page in pages)
            usable = any(page.blocks for page in pages)
            run.status = ("partial" if usable else "failed") if failed else "completed"
            run.error_code = error_code
            run.finished_at = func.now()
            return run.status

    @staticmethod
    def _details(run):
        return {"run_id": str(run.id), "status": run.status, "source_hash": run.source_hash,
                "source_type": "policy" if run.policy_document_id is not None else "claim",
                "document_id": str(run.policy_document_id or run.claim_document_id),
                "policy_document_version": run.policy_document_version,
                "extractor_version": run.extractor_version, "config": run.config, "page_count": run.page_count,
                "error_code": run.error_code, "created_at": run.created_at.isoformat(),
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "pages": [{"page_number": page.page_number, "status": page.status,
                           "method": page.method, "reason": page.reason, "error": page.error,
                           "width": page.width, "height": page.height, "rotation": page.rotation,
                           "preview": " ".join(block.raw_text for block in page.blocks)[:160],
                           "blocks": [{"index": block.block_index, "method": block.method,
                                       "bbox": block.bbox, "confidence": block.confidence,
                                       "preview": block.raw_text[:160]} for block in page.blocks]}
                          for page in run.pages]}

    def get_run(self, run_id):
        with self.sessions() as session:
            run = session.scalar(select(DocumentProcessingRun).where(DocumentProcessingRun.id == run_id)
                .options(selectinload(DocumentProcessingRun.pages).selectinload(ExtractedPage.blocks)))
            if run is None:
                raise PdfInputError("run_not_found")
            return self._details(run)

    def inspect(self, source_type, document_id):
        if source_type not in ("policy", "claim"):
            raise PdfInputError("invalid_source_type")
        model = PolicyDocument if source_type == "policy" else ClaimDocument
        source_column = (DocumentProcessingRun.policy_document_id if source_type == "policy"
                         else DocumentProcessingRun.claim_document_id)
        with self.sessions() as session:
            if session.get(model, document_id) is None:
                raise PdfInputError("document_not_found")
            runs = session.scalars(select(DocumentProcessingRun).where(source_column == document_id)
                .options(selectinload(DocumentProcessingRun.pages).selectinload(ExtractedPage.blocks))
                .order_by(DocumentProcessingRun.created_at.desc(), DocumentProcessingRun.id)).all()
            return [self._details(run) for run in runs]
