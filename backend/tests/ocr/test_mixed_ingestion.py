"""Run the CLI on a synthetic mixed PDF with PostgreSQL and real Tesseract."""
import json
import os
from uuid import uuid4

import pymupdf
import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker

from app import cli
from app.domain.models import Claim, ClaimDocument, ClaimStatus, DocumentProcessingRun, Organization, ProcessingStatus


@pytest.fixture
def source(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url or url == os.environ.get("DATABASE_URL"):
        pytest.fail("TEST_DATABASE_URL must be a separate migrated PostgreSQL database")
    engine = create_engine(url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    org_id, claim_id, document_id = uuid4(), uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(Organization(id=org_id, name="OCR integration", code=org_id.hex))
        session.flush()
        session.add(Claim(id=claim_id, organization_id=org_id, claim_number="OCR", description="Synthetic claim", status=ClaimStatus.DRAFT))
        session.flush()
        session.add(ClaimDocument(id=document_id, claim_id=claim_id, document_type="report", filename="mixed.pdf", content_type="application/pdf", storage_key="synthetic", processing_status=ProcessingStatus.PENDING))
    monkeypatch.setattr(cli, "make_sessions", lambda: sessions)
    yield sessions, document_id
    with sessions.begin() as session:
        session.execute(delete(DocumentProcessingRun).where(DocumentProcessingRun.claim_document_id == document_id))
        session.execute(delete(ClaimDocument).where(ClaimDocument.id == document_id))
        session.execute(delete(Claim).where(Claim.id == claim_id))
        session.execute(delete(Organization).where(Organization.id == org_id))
    engine.dispose()


def test_cli_ingests_mixed_pdf_and_reuses_run(source, monkeypatch, tmp_path, capsys):
    sessions, document_id = source
    monkeypatch.setenv("PDF_STORAGE_DIR", str(tmp_path / "storage"))
    for name, value in {"PDF_OCR_LANGUAGE": "eng", "PDF_OCR_DPI": "200", "PDF_OCR_TIMEOUT_SECONDS": "60",
                        "PDF_OCR_MAX_PIXELS": "25000000", "PDF_NATIVE_TIMEOUT_SECONDS": "30",
                        "PDF_MAX_BYTES": "26214400", "PDF_MAX_PAGES": "200"}.items():
        monkeypatch.setenv(name, value)
    drawing = pymupdf.open()
    drawing_page = drawing.new_page(width=500, height=160)
    drawing_page.insert_text((30, 60), "CLAIM NUMBER ABC123", fontsize=26)
    image = drawing_page.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "POLICY NUMBER P123")
    scan = pdf.new_page(width=500, height=160)
    scan.insert_image(scan.rect, stream=image)
    path = tmp_path / "mixed.pdf"
    pdf.save(path)
    pdf.close()
    drawing.close()
    command = ["ingest-pdf", "--source-type", "claim", "--document-id", str(document_id), "--file", str(path)]
    assert cli.main(command) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed"
    assert result["source_type"] == "claim" and result["document_id"] == str(document_id)
    assert [page["method"] for page in result["pages"]] == ["native", "ocr"]
    assert "P123" in result["pages"][0]["preview"]
    assert "ABC123" in result["pages"][1]["preview"]
    assert result["pages"][1]["blocks"][0]["bbox"]
    assert cli.main(command) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["run_id"] == result["run_id"]
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(DocumentProcessingRun).where(DocumentProcessingRun.claim_document_id == document_id)) == 1
        assert session.get(ClaimDocument, document_id).processing_status == ProcessingStatus.COMPLETED
