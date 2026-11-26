"""Transactional storage for extracted document evidence."""
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.domain.models import ClaimDocument, PolicyDocument, DocumentProcessingRun, ExtractedPage, ExtractedBlock
from app.services.pdf_input import PdfInputError


class DocumentRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    def create(self, source, config, config_hash, version):
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
        with self.sessions.begin() as session:
            result = session.execute(update(DocumentProcessingRun).where(
                DocumentProcessingRun.id == run_id, DocumentProcessingRun.status == "pending"
            ).values(status="processing", started_at=func.now()))
            if result.rowcount != 1:
                raise PdfInputError("run_not_pending")

    def save_page(self, run_id, page):
        with self.sessions.begin() as session:
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
            return [{"run_id": str(run.id), "status": run.status, "source_hash": run.source_hash,
                     "extractor_version": run.extractor_version, "page_count": run.page_count,
                     "error_code": run.error_code, "created_at": run.created_at.isoformat(),
                     "started_at": run.started_at.isoformat() if run.started_at else None,
                     "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                     "pages": [{"page_number": page.page_number, "status": page.status,
                                "method": page.method, "reason": page.reason, "error": page.error,
                                "preview": " ".join(block.raw_text for block in page.blocks)[:160]}
                               for page in run.pages]} for run in runs]
