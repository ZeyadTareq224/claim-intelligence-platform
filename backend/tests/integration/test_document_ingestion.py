"""Run against an isolated, migrated PostgreSQL database."""
import hashlib
import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pymupdf
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.domain.models import (Claim, ClaimDocument, ClaimStatus, DocumentProcessingRun, ExtractedPage,
                               Organization, Policy, PolicyDocument, PolicyStatus, PolicyType, ProcessingStatus, Vehicle)
from app.repositories.documents import DocumentRepository
from app.services.document_ingestion import DocumentIngestionService
from app.services.pdf_extraction import PdfExtractor
from app.services.pdf_input import PdfInputService


@pytest.fixture
def sessions():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to an isolated migrated PostgreSQL database")
    if url == os.environ.get("DATABASE_URL"):
        pytest.fail("TEST_DATABASE_URL must differ from DATABASE_URL")
    engine = create_engine(url)
    connection = engine.connect()
    transaction = connection.begin()
    yield sessionmaker(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    transaction.rollback()
    connection.close()
    engine.dispose()


def make_source(sessions, source_type, version=1):
    org_id, vehicle_id, policy_id, claim_id, document_id = [uuid4() for _ in range(5)]
    with sessions.begin() as session:
        session.add(Organization(id=org_id, name="Document test", code=org_id.hex))
        session.flush()
        if source_type == "policy":
            session.add(Vehicle(id=vehicle_id, organization_id=org_id, plate_number="T", make="T", model="T", manufacture_year=2020))
            session.flush()
            session.add(Policy(id=policy_id, organization_id=org_id, vehicle_id=vehicle_id, policy_number="P1",
                policy_type=PolicyType.MOTOR_COMPREHENSIVE, status=PolicyStatus.ACTIVE,
                effective_date=date(2024, 1, 1), expiration_date=date(2025, 1, 1), sum_insured=Decimal("100"), currency="EGP"))
            session.flush()
            session.add(PolicyDocument(id=document_id, policy_id=policy_id, filename="test.pdf", content_type="application/pdf", storage_key="original", document_version=version))
        else:
            session.add(Claim(id=claim_id, organization_id=org_id, claim_number="C1", description="Claim", status=ClaimStatus.DRAFT))
            session.flush()
            session.add(ClaimDocument(id=document_id, claim_id=claim_id, document_type="report", filename="test.pdf", content_type="application/pdf", storage_key="original", processing_status=ProcessingStatus.PENDING))
    return document_id


def make_pdf(path, text="Policy P-123 amount EGP 100"):
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), text)
    pdf.new_page()
    data = pdf.tobytes()
    pdf.close()
    path.write_bytes(data)
    return data


def ingestion(sessions, tmp_path):
    return DocumentIngestionService(PdfInputService(sessions, tmp_path / "storage"), PdfExtractor(), DocumentRepository(sessions))


@pytest.mark.parametrize("source_type", ["policy", "claim"])
def test_persists_document_version_hash_pages_and_raw_blocks(sessions, tmp_path, source_type):
    document_id = make_source(sessions, source_type, version=3)
    path = tmp_path / "input.pdf"
    data = make_pdf(path)
    service = ingestion(sessions, tmp_path)
    run_id = service.ingest(source_type, document_id, path)
    with sessions() as session:
        run = session.get(DocumentProcessingRun, run_id)
        assert run.source_hash == hashlib.sha256(data).hexdigest()
        assert run.policy_document_id == (document_id if source_type == "policy" else None)
        assert run.claim_document_id == (document_id if source_type == "claim" else None)
        assert run.policy_document_version == (3 if source_type == "policy" else None)
        assert run.status == "completed" and run.page_count == 2
        assert run.extractor_version == run.config["extractor_version"] and run.created_at
        assert [page.page_number for page in run.pages] == [1, 2]
        assert [page.status for page in run.pages] == ["completed", "blank"]
        first = run.pages[0]
        assert first.method == "native" and first.width > 0 and first.height > 0
        assert first.blocks[0].block_index == 0 and "P-123" in first.blocks[0].raw_text
        assert first.blocks[0].normalized_text == " ".join(first.blocks[0].raw_text.split())
        assert first.blocks[0].bbox and first.blocks[0].method == "native"
    assert (service.inputs.storage_dir / f"{hashlib.sha256(data).hexdigest()}.pdf").read_bytes() == data


def test_constraints_prevent_duplicate_run_and_cross_document_link(sessions, tmp_path):
    first = make_source(sessions, "policy")
    second = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    first_run = service.ingest("policy", first, path)
    second_run = service.ingest("claim", second, path)
    with pytest.raises(IntegrityError):
        service.ingest("policy", first, path)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(DocumentProcessingRun)) == 2
        assert session.get(DocumentProcessingRun, first_run).claim_document_id is None
        assert session.get(DocumentProcessingRun, second_run).policy_document_id is None


def test_new_bytes_produce_a_separate_auditable_run(sessions, tmp_path):
    document_id = make_source(sessions, "policy")
    path = tmp_path / "test.pdf"
    service = ingestion(sessions, tmp_path)
    make_pdf(path, "Version one")
    first = service.ingest("policy", document_id, path)
    make_pdf(path, "Version two")
    second = service.ingest("policy", document_id, path)
    with sessions() as session:
        assert session.get(DocumentProcessingRun, first).source_hash != session.get(DocumentProcessingRun, second).source_hash
        assert session.scalar(select(func.count()).select_from(ExtractedPage)) == 4


def test_page_failure_is_visible_and_successful_blocks_are_saved(sessions, tmp_path):
    from app.services.pdf_extraction import PageResult, TextBlock

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    service.extractor.extract_bytes = lambda data: [
        PageResult(1, "completed", "native", 200, 200, 0, [TextBlock("  Kept\n  text  ", "native", (1, 2, 3, 4))]),
        PageResult(2, "failed", "ocr", 200, 200, 0, error="ocr_timeout"),
    ]
    run_id = service.ingest("claim", document_id, path)
    with sessions() as session:
        run = session.get(DocumentProcessingRun, run_id)
        assert run.status == "partial" and run.pages[1].error == "ocr_timeout"
        assert run.pages[0].blocks[0].raw_text == "  Kept\n  text  "
        assert run.pages[0].blocks[0].normalized_text == "Kept text"


def test_tampered_preserved_source_is_rejected_before_extraction(sessions, tmp_path):
    from app.services.pdf_input import PdfInputError

    document_id = make_source(sessions, "policy")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    validate = service.inputs.validate

    def tamper(*args):
        source = validate(*args)
        (service.inputs.storage_dir / f"{source.sha256}.pdf").write_bytes(b"changed")
        return source

    service.inputs.validate = tamper
    with pytest.raises(PdfInputError, match="storage_conflict"):
        service.ingest("policy", document_id, path)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(DocumentProcessingRun)) == 0


def test_failed_persistence_rolls_back_all_page_records(sessions, tmp_path):
    from app.services.pdf_extraction import PageResult, TextBlock

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    service.extractor.extract_bytes = lambda data: [
        PageResult(1, "completed", "native", 200, 200, 0, [TextBlock("valid", "native")]),
        PageResult(2, "completed", "native", 200, 200, 0, [TextBlock("bad", "invalid")]),
    ]
    with pytest.raises(IntegrityError):
        service.ingest("claim", document_id, path)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(DocumentProcessingRun)) == 0
        assert session.scalar(select(func.count()).select_from(ExtractedPage)) == 0


def test_extractor_version_creates_a_distinct_run(sessions, tmp_path, monkeypatch):
    document_id = make_source(sessions, "policy")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    first = service.ingest("policy", document_id, path)
    monkeypatch.setattr("app.services.document_ingestion.EXTRACTOR_VERSION", "test-version-2")
    second = service.ingest("policy", document_id, path)
    with sessions() as session:
        one, two = session.get(DocumentProcessingRun, first), session.get(DocumentProcessingRun, second)
        assert one.source_hash == two.source_hash and one.extractor_version != two.extractor_version
        assert one.config_hash != two.config_hash


def test_database_rejects_two_sources_on_one_run(sessions):
    policy_id = make_source(sessions, "policy")
    claim_id = make_source(sessions, "claim")
    with pytest.raises(IntegrityError):
        with sessions.begin() as session:
            session.add(DocumentProcessingRun(policy_document_id=policy_id, claim_document_id=claim_id,
                policy_document_version=1, source_hash="a" * 64, config_hash="b" * 64,
                extractor_version="test", config={}, status="completed", page_count=1))
            session.flush()
