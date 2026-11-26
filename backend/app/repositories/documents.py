"""Transactional storage for extracted document evidence."""
from app.domain.models import ClaimDocument, PolicyDocument, DocumentProcessingRun, ExtractedPage, ExtractedBlock
from app.services.pdf_input import PdfInputError


class DocumentRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    def save(self, source, config, config_hash, version, pages):
        with self.sessions.begin() as session:
            model = PolicyDocument if source.source_type == "policy" else ClaimDocument
            document = session.get(model, source.document_id, with_for_update=True)
            if document is None:
                raise PdfInputError("document_not_found")
            if source.source_type == "policy" and document.document_version != source.policy_document_version:
                raise PdfInputError("document_version_changed")
            failed = any(page.status == "failed" for page in pages)
            usable = any(page.blocks for page in pages)
            status = ("partial" if usable else "failed") if failed else "completed"
            run = DocumentProcessingRun(
                policy_document_id=source.document_id if source.source_type == "policy" else None,
                claim_document_id=source.document_id if source.source_type == "claim" else None,
                policy_document_version=source.policy_document_version,
                source_hash=source.sha256, extractor_version=version, config_hash=config_hash,
                config=config, status=status, page_count=source.page_count,
                pages=[ExtractedPage(
                    page_number=page.page_number, status=page.status, method=page.method,
                    reason=page.reason, error=page.error, width=page.width, height=page.height,
                    rotation=page.rotation,
                    blocks=[ExtractedBlock(block_index=index, raw_text=block.text,
                        normalized_text=" ".join(block.text.split()), block_type="unknown", method=block.method,
                        bbox=list(block.bbox) if block.bbox is not None else None, confidence=block.confidence)
                        for index, block in enumerate(page.blocks)],
                ) for page in pages],
            )
            session.add(run)
            session.flush()
            run_id = run.id
        return run_id
