"""Run against an isolated, migrated PostgreSQL database."""
import hashlib
import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pymupdf
import pytest
from sqlalchemy import create_engine, delete, func, select, or_
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
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    factory.test_org_ids = []
    yield factory
    with factory.begin() as session:
        org_ids = factory.test_org_ids
        policy_ids = select(Policy.id).where(Policy.organization_id.in_(org_ids))
        claim_ids = select(Claim.id).where(Claim.organization_id.in_(org_ids))
        policy_docs = select(PolicyDocument.id).where(PolicyDocument.policy_id.in_(policy_ids))
        claim_docs = select(ClaimDocument.id).where(ClaimDocument.claim_id.in_(claim_ids))
        session.execute(delete(DocumentProcessingRun).where(or_(
            DocumentProcessingRun.policy_document_id.in_(policy_docs),
            DocumentProcessingRun.claim_document_id.in_(claim_docs))))
        session.execute(delete(PolicyDocument).where(PolicyDocument.id.in_(policy_docs)))
        session.execute(delete(ClaimDocument).where(ClaimDocument.id.in_(claim_docs)))
        session.execute(delete(Claim).where(Claim.id.in_(claim_ids)))
        session.execute(delete(Policy).where(Policy.id.in_(policy_ids)))
        session.execute(delete(Vehicle).where(Vehicle.organization_id.in_(org_ids)))
        session.execute(delete(Organization).where(Organization.id.in_(org_ids)))
    engine.dispose()


def make_source(sessions, source_type, version=1):
    org_id, vehicle_id, policy_id, claim_id, document_id = [uuid4() for _ in range(5)]
    sessions.test_org_ids.append(org_id)
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
    assert service.ingest("policy", first, path) == first_run
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
    service.extractor.iter_bytes = lambda data, page_numbers=None: [
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


def test_failed_persistence_keeps_committed_page_records(sessions, tmp_path):
    from app.services.pdf_extraction import PageResult, TextBlock

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    service.extractor.iter_bytes = lambda data, page_numbers=None: [
        PageResult(1, "completed", "native", 200, 200, 0, [TextBlock("valid", "native")]),
        PageResult(2, "completed", "native", 200, 200, 0, [TextBlock("bad", "invalid")]),
    ]
    with pytest.raises(IntegrityError):
        service.ingest("claim", document_id, path)
    with sessions() as session:
        run = session.scalar(select(DocumentProcessingRun))
        assert run.status == "partial" and run.error_code == "persistence_failed"
        assert len(run.pages) == 1 and run.pages[0].blocks[0].raw_text == "valid"


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


def test_processing_is_visible_and_pages_survive_interruption(sessions, tmp_path):
    from app.services.pdf_extraction import PageResult, TextBlock

    document_id = make_source(sessions, "policy")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    repository = service.repository

    def interrupted(data, page_numbers=None):
        runs = repository.inspect("policy", document_id)
        assert runs[0]["status"] == "processing" and runs[0]["pages"] == []
        yield PageResult(1, "completed", "native", 200, 200, 0, [TextBlock("saved page", "native")])
        runs = repository.inspect("policy", document_id)
        assert runs[0]["status"] == "processing"
        assert runs[0]["pages"][0]["preview"] == "saved page"
        raise KeyboardInterrupt()

    service.extractor.iter_bytes = interrupted
    with pytest.raises(KeyboardInterrupt):
        service.ingest("policy", document_id, path)
    runs = repository.inspect("policy", document_id)
    assert len(runs) == 1 and runs[0]["status"] == "processing"
    assert len(runs[0]["pages"]) == 1 and runs[0]["finished_at"] is None


def test_unexpected_extraction_error_marks_partial(sessions, tmp_path):
    from app.services.pdf_extraction import PageResult, TextBlock

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)

    def broken(data, page_numbers=None):
        yield PageResult(1, "completed", "native", 200, 200, 0, [TextBlock("saved", "native")])
        raise RuntimeError("sensitive content should not be saved as an error")

    service.extractor.iter_bytes = broken
    with pytest.raises(RuntimeError):
        service.ingest("claim", document_id, path)
    result = service.repository.inspect("claim", document_id)[0]
    assert result["status"] == "partial" and result["error_code"] == "extraction_failed"
    assert result["finished_at"] and result["pages"][0]["preview"] == "saved"
    with sessions() as session:
        assert session.get(ClaimDocument, document_id).processing_status == ProcessingStatus.PARTIAL


def test_incomplete_extraction_cannot_complete(sessions, tmp_path):
    from app.services.pdf_extraction import PageResult

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    service.extractor.iter_bytes = lambda data, page_numbers=None: [PageResult(1, "blank", None, 200, 200, 0)]
    run_id = service.ingest("claim", document_id, path)
    with sessions() as session:
        run = session.get(DocumentProcessingRun, run_id)
        assert run.status == "failed" and run.error_code == "page_count_mismatch"


def test_cli_inspects_processing_results(sessions, tmp_path, monkeypatch, capsys):
    import json
    from app import cli

    document_id = make_source(sessions, "policy")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    run_id = ingestion(sessions, tmp_path).ingest("policy", document_id, path)
    monkeypatch.setattr(cli, "make_sessions", lambda: sessions)
    assert cli.main(["inspect-document", "--source-type", "policy", "--document-id", str(document_id)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["runs"][0]["run_id"] == str(run_id)
    assert result["runs"][0]["status"] == "completed"
    assert result["runs"][0]["pages"][0]["method"] == "native"


def test_pending_run_remains_visible_if_interrupted_before_start(sessions, tmp_path, monkeypatch):
    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)

    def interrupt(self, run_id):
        raise KeyboardInterrupt()

    monkeypatch.setattr(DocumentRepository, "start", interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.ingest("claim", document_id, path)
    result = service.repository.inspect("claim", document_id)[0]
    assert result["status"] == "pending" and result["started_at"] is None
    assert result["pages"] == []


def test_ocr_failure_with_no_usable_text_is_failed(sessions, tmp_path):
    from app.services.pdf_extraction import PageResult

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    service.extractor.iter_bytes = lambda data, page_numbers=None: [
        PageResult(1, "failed", "ocr", 200, 200, 0, error="ocr_timeout"),
        PageResult(2, "blank", None, 200, 200, 0),
    ]
    service.ingest("claim", document_id, path)
    result = service.repository.inspect("claim", document_id)[0]
    assert result["status"] == "failed" and result["pages"][0]["error"] == "ocr_timeout"


def test_completed_repeat_returns_same_run_without_extracting(sessions, tmp_path):
    document_id = make_source(sessions, "policy")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    first = service.ingest("policy", document_id, path)

    def no_extract(*args):
        pytest.fail("completed pages should not be extracted again")

    service.extractor.iter_bytes = no_extract
    assert service.ingest("policy", document_id, path) == first
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(DocumentProcessingRun)) == 1
        assert session.scalar(select(func.count()).select_from(ExtractedPage)) == 2


def test_retry_failed_page_preserves_successful_page_and_block(sessions, tmp_path):
    from app.services.pdf_extraction import PageResult, TextBlock

    document_id = make_source(sessions, "policy")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    service.extractor.iter_bytes = lambda data, page_numbers: iter([
        PageResult(1, "completed", "native", 200, 200, 0, [TextBlock("first", "native")]),
        PageResult(2, "failed", "ocr", 200, 200, 0, error="ocr_timeout"),
    ])
    run_id = service.ingest("policy", document_id, path)
    with sessions() as session:
        run = session.get(DocumentProcessingRun, run_id)
        page_id, block_id = run.pages[0].id, run.pages[0].blocks[0].id
        assert run.status == "partial"

    def retry(data, page_numbers):
        assert page_numbers == {2}
        yield PageResult(2, "completed", "ocr", 200, 200, 0, [TextBlock("recovered", "ocr")])

    service.extractor.iter_bytes = retry
    assert service.ingest("policy", document_id, path) == run_id
    with sessions() as session:
        run = session.get(DocumentProcessingRun, run_id)
        assert run.status == "completed" and run.error_code is None
        assert run.pages[0].id == page_id and run.pages[0].blocks[0].id == block_id
        assert run.pages[1].blocks[0].raw_text == "recovered"
        assert len(run.pages) == 2


def test_retry_after_interruption_keeps_completed_page(sessions, tmp_path):
    from app.services.pdf_extraction import PageResult, TextBlock

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)

    def interrupted(data, page_numbers):
        yield PageResult(1, "completed", "native", 200, 200, 0, [TextBlock("saved", "native")])
        raise KeyboardInterrupt()

    service.extractor.iter_bytes = interrupted
    with pytest.raises(KeyboardInterrupt):
        service.ingest("claim", document_id, path)
    before = service.repository.inspect("claim", document_id)[0]
    with sessions() as session:
        assert session.get(ClaimDocument, document_id).processing_status == ProcessingStatus.PROCESSING

    def resume(data, page_numbers):
        assert page_numbers == {2}
        yield PageResult(2, "blank", None, 200, 200, 0)

    service.extractor.iter_bytes = resume
    run_id = service.ingest("claim", document_id, path)
    assert str(run_id) == before["run_id"]
    result = service.repository.inspect("claim", document_id)[0]
    assert result["status"] == "completed" and result["pages"][0]["preview"] == "saved"
    with sessions() as session:
        assert session.get(ClaimDocument, document_id).processing_status == ProcessingStatus.COMPLETED


def test_concurrent_attempt_is_rejected_without_duplicate_run(sessions, tmp_path):
    from app.services.pdf_input import PdfInputError

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    original = service.extractor.iter_bytes

    def competing_attempt(data, page_numbers):
        with pytest.raises(PdfInputError, match="processing_in_progress"):
            service.ingest("claim", document_id, path)
        yield from original(data, page_numbers)

    service.extractor.iter_bytes = competing_attempt
    run_id = service.ingest("claim", document_id, path)
    with sessions() as session:
        assert session.get(DocumentProcessingRun, run_id).status == "completed"
        assert session.scalar(select(func.count()).select_from(DocumentProcessingRun)) == 1


def test_retry_failure_keeps_previous_partial_page_text(sessions, tmp_path):
    from app.services.pdf_extraction import PageResult, TextBlock

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    service.extractor.iter_bytes = lambda data, page_numbers: iter([
        PageResult(1, "failed", "mixed", 200, 200, 0, [TextBlock("keep me", "native")], error="ocr_timeout"),
        PageResult(2, "blank", None, 200, 200, 0),
    ])
    run_id = service.ingest("claim", document_id, path)
    service.extractor.iter_bytes = lambda data, page_numbers: iter([
        PageResult(1, "failed", "ocr", 200, 200, 0, error="ocr_unavailable"),
    ])
    assert service.ingest("claim", document_id, path) == run_id
    result = service.repository.inspect("claim", document_id)[0]
    assert result["status"] == "partial"
    assert result["pages"][0]["preview"] == "keep me"
    assert result["pages"][0]["error"] == "ocr_unavailable"


def test_lost_claim_connection_stops_writes_and_allows_resume(sessions, tmp_path, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    from app.services.pdf_extraction import PageResult, TextBlock
    from app.services.pdf_input import PdfInputError

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    original_save = DocumentRepository.save_page
    backend_pid = []

    def capture(self, run_id, page):
        original_save(self, run_id, page)
        if not backend_pid:
            backend_pid.append(self.claim_connection.scalar(select(func.pg_backend_pid())))
            self.claim_connection.commit()

    monkeypatch.setattr(DocumentRepository, "save_page", capture)

    def disconnected(data, page_numbers):
        yield PageResult(1, "completed", "native", 200, 200, 0, [TextBlock("saved", "native")])
        with sessions.begin() as session:
            assert session.scalar(select(func.pg_terminate_backend(backend_pid[0])))
        yield PageResult(2, "blank", None, 200, 200, 0)

    service.extractor.iter_bytes = disconnected
    with pytest.raises((SQLAlchemyError, PdfInputError)):
        service.ingest("claim", document_id, path)
    result = service.repository.inspect("claim", document_id)[0]
    assert result["status"] == "processing" and len(result["pages"]) == 1
    with sessions() as session:
        assert session.get(ClaimDocument, document_id).processing_status == ProcessingStatus.PROCESSING
    monkeypatch.setattr(DocumentRepository, "save_page", original_save)
    service.extractor.iter_bytes = lambda data, page_numbers: iter([PageResult(2, "blank", None, 200, 200, 0)])
    assert str(service.ingest("claim", document_id, path)) == result["run_id"]
    assert service.repository.inspect("claim", document_id)[0]["status"] == "completed"


def test_failed_replacement_rolls_back_to_previous_page(sessions, tmp_path):
    from app.services.pdf_extraction import PageResult, TextBlock

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    service.extractor.iter_bytes = lambda data, page_numbers: iter([
        PageResult(1, "failed", "mixed", 200, 200, 0, [TextBlock("keep old", "native")], error="ocr_timeout"),
        PageResult(2, "blank", None, 200, 200, 0),
    ])
    run_id = service.ingest("claim", document_id, path)
    service.extractor.iter_bytes = lambda data, page_numbers: iter([
        PageResult(1, "completed", "native", 200, 200, 0, [TextBlock("invalid", "bad_method")]),
    ])
    with pytest.raises(IntegrityError):
        service.ingest("claim", document_id, path)
    result = service.repository.inspect("claim", document_id)[0]
    assert result["run_id"] == str(run_id) and result["error_code"] == "persistence_failed"
    assert result["pages"][0]["preview"] == "keep old"


def test_cli_returns_nonzero_for_partial_result(sessions, tmp_path, monkeypatch, capsys):
    import json
    from app import cli
    from app.services.pdf_extraction import PageResult, TextBlock

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    service.extractor.iter_bytes = lambda data, page_numbers: iter([
        PageResult(1, "completed", "native", 200, 200, 0, [TextBlock("saved", "native")]),
        PageResult(2, "failed", "ocr", 200, 200, 0, error="ocr_timeout"),
    ])
    monkeypatch.setattr(cli, "make_sessions", lambda: sessions)
    monkeypatch.setattr(cli.DocumentIngestionService, "from_environment", lambda _: service)
    assert cli.main(["ingest-pdf", "--source-type", "claim", "--document-id", str(document_id), "--file", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "partial" and result["pages"][1]["error"] == "ocr_timeout"


def test_cli_rejects_invalid_pdf_without_a_run(sessions, tmp_path, monkeypatch, capsys):
    from app import cli

    document_id = make_source(sessions, "claim")
    path = tmp_path / "invalid.pdf"
    path.write_bytes(b"not a PDF")
    monkeypatch.setattr(cli, "make_sessions", lambda: sessions)
    monkeypatch.setenv("PDF_STORAGE_DIR", str(tmp_path / "storage"))
    assert cli.main(["ingest-pdf", "--source-type", "claim", "--document-id", str(document_id), "--file", str(path)]) == 1
    assert "invalid_pdf" in capsys.readouterr().err
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(DocumentProcessingRun).where(DocumentProcessingRun.claim_document_id == document_id)) == 0


def test_native_timeout_marks_only_its_page_and_keeps_other_pages(sessions, tmp_path, monkeypatch):
    import subprocess
    import sys

    document_id = make_source(sessions, "policy")
    path = tmp_path / "three-pages.pdf"
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "First policy page")
    pdf.new_page().insert_text((72, 72), "Second policy page")
    pdf.new_page().insert_text((72, 72), "Third policy page")
    pdf.save(path)
    pdf.close()
    service = ingestion(sessions, tmp_path)
    service.extractor.native_timeout = 2
    start_worker = service.extractor._start_worker

    def worker(pdf_path, image_path, number):
        if number == 2:
            return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return start_worker(pdf_path, image_path, number)

    monkeypatch.setattr(service.extractor, "_start_worker", worker)
    run_id = service.ingest("policy", document_id, path)
    with sessions() as session:
        run = session.get(DocumentProcessingRun, run_id)
        assert run.status == "partial" and run.config["native_timeout"] == 2
        assert [page.status for page in run.pages] == ["completed", "failed", "completed"]
        assert run.pages[1].error == "native_timeout"
        assert "First" in run.pages[0].blocks[0].raw_text
        assert "Third" in run.pages[2].blocks[0].raw_text


def test_changing_native_timeout_creates_distinct_run(sessions, tmp_path):
    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    first = service.ingest("claim", document_id, path)
    service.extractor.native_timeout = 35
    second = service.ingest("claim", document_id, path)
    with sessions() as session:
        first_run, second_run = session.get(DocumentProcessingRun, first), session.get(DocumentProcessingRun, second)
        assert first != second and first_run.source_hash == second_run.source_hash
        assert first_run.config_hash != second_run.config_hash
        assert first_run.config["native_timeout"] == 30 and second_run.config["native_timeout"] == 35


def test_processing_logs_identifiers_and_counts_without_document_text(sessions, tmp_path, caplog):
    import logging

    document_id = make_source(sessions, "policy")
    path = tmp_path / "private-name.pdf"
    make_pdf(path, "Sensitive synthetic policy text")
    service = ingestion(sessions, tmp_path)
    with caplog.at_level(logging.INFO, logger="app.services.document_ingestion"):
        run_id = service.ingest("policy", document_id, path)
        assert service.ingest("policy", document_id, path) == run_id
    logs = caplog.text
    assert str(document_id) in logs and str(run_id) in logs
    assert "page_count=2" in logs and "methods=" in logs and "duration_ms=" in logs
    assert "document_processing_reused" in logs
    assert "Sensitive synthetic policy text" not in logs
    assert "private-name.pdf" not in logs


def test_inspection_includes_full_text_only_when_requested(sessions, tmp_path, monkeypatch, capsys):
    import json
    from app import cli

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path, "Text visible only when explicitly requested")
    service = ingestion(sessions, tmp_path)
    service.ingest("claim", document_id, path)
    monkeypatch.setattr(cli, "make_sessions", lambda: sessions)
    args = ["inspect-document", "--source-type", "claim", "--document-id", str(document_id)]
    assert cli.main(args) == 0
    default = json.loads(capsys.readouterr().out)
    assert "raw_text" not in default["runs"][0]["pages"][0]["blocks"][0]
    assert cli.main(args + ["--full-text"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert "Text visible only" in full["runs"][0]["pages"][0]["blocks"][0]["raw_text"]


def test_changing_tesseract_version_creates_distinct_run(sessions, tmp_path, monkeypatch):
    document_id = make_source(sessions, "policy")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    monkeypatch.setattr(service.extractor.ocr, "engine_version", lambda: "5.0")
    first = service.ingest("policy", document_id, path)
    monkeypatch.setattr(service.extractor.ocr, "engine_version", lambda: "5.1")
    second = service.ingest("policy", document_id, path)
    with sessions() as session:
        one, two = session.get(DocumentProcessingRun, first), session.get(DocumentProcessingRun, second)
        assert first != second and one.config_hash != two.config_hash
        assert one.config["tesseract_version"] == "5.0" and two.config["tesseract_version"] == "5.1"


def test_processing_logs_categorized_page_failures(sessions, tmp_path, caplog):
    import logging
    from app.services.pdf_extraction import PageResult, TextBlock

    document_id = make_source(sessions, "claim")
    path = tmp_path / "private.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    service.extractor.iter_bytes = lambda data, page_numbers: iter([
        PageResult(1, "completed", "native", 200, 200, 0, [TextBlock("Private page text", "native")]),
        PageResult(2, "failed", "ocr", 200, 200, 0, error="ocr_timeout"),
    ])
    with caplog.at_level(logging.INFO, logger="app.services.document_ingestion"):
        service.ingest("claim", document_id, path)
    assert "status=partial" in caplog.text and "ocr_timeout" in caplog.text
    assert "Private page text" not in caplog.text and "private.pdf" not in caplog.text


@pytest.mark.parametrize("page_one,page_two,expected", [
    ("completed", "blank", ProcessingStatus.COMPLETED),
    ("completed", "failed", ProcessingStatus.PARTIAL),
    ("failed", "blank", ProcessingStatus.FAILED),
])
def test_claim_document_status_tracks_run_outcome(sessions, tmp_path, page_one, page_two, expected):
    from app.services.pdf_extraction import PageResult, TextBlock

    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)

    def pages(data, page_numbers):
        with sessions() as session:
            assert session.get(ClaimDocument, document_id).processing_status == ProcessingStatus.PROCESSING
        blocks = [TextBlock("kept", "native")] if page_one == "completed" else []
        yield PageResult(1, page_one, "native" if blocks else "ocr", 200, 200, 0, blocks,
                         error="ocr_timeout" if page_one == "failed" else None)
        yield PageResult(2, page_two, "ocr" if page_two == "failed" else None, 200, 200, 0,
                         error="ocr_timeout" if page_two == "failed" else None)

    service.extractor.iter_bytes = pages
    run_id = service.ingest("claim", document_id, path)
    with sessions() as session:
        run = session.get(DocumentProcessingRun, run_id)
        document = session.get(ClaimDocument, document_id)
        assert run.status == expected.value
        assert document.processing_status == expected


def test_claim_status_is_pending_if_interrupted_before_start(sessions, tmp_path, monkeypatch):
    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    with sessions.begin() as session:
        session.get(ClaimDocument, document_id).processing_status = ProcessingStatus.FAILED
    service = ingestion(sessions, tmp_path)

    def interrupt(self, run_id):
        raise KeyboardInterrupt()

    monkeypatch.setattr(DocumentRepository, "start", interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.ingest("claim", document_id, path)
    with sessions() as session:
        run = session.scalar(select(DocumentProcessingRun).where(DocumentProcessingRun.claim_document_id == document_id))
        assert run.status == "pending"
        assert session.get(ClaimDocument, document_id).processing_status == ProcessingStatus.PENDING


def test_claim_status_updates_when_an_older_completed_run_is_reused(sessions, tmp_path):
    from app.services.pdf_extraction import PageResult

    document_id = make_source(sessions, "claim")
    original = tmp_path / "original.pdf"
    later = tmp_path / "later.pdf"
    make_pdf(original, "First document")
    make_pdf(later, "Different document")
    service = ingestion(sessions, tmp_path)
    completed_id = service.ingest("claim", document_id, original)
    service.extractor.iter_bytes = lambda data, page_numbers: iter([
        PageResult(1, "failed", "ocr", 200, 200, 0, error="ocr_timeout"),
        PageResult(2, "blank", None, 200, 200, 0),
    ])
    failed_id = service.ingest("claim", document_id, later)
    with sessions() as session:
        assert session.get(ClaimDocument, document_id).processing_status == ProcessingStatus.FAILED
    service.extractor.iter_bytes = lambda *args: pytest.fail("completed run must be reused")
    assert service.ingest("claim", document_id, original) == completed_id
    with sessions() as session:
        assert session.get(ClaimDocument, document_id).processing_status == ProcessingStatus.COMPLETED
        assert session.get(DocumentProcessingRun, failed_id).status == "failed"


def test_concurrent_different_sources_for_same_claim_are_rejected(sessions, tmp_path):
    from app.services.pdf_input import PdfInputError

    document_id = make_source(sessions, "claim")
    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    make_pdf(first, "First version")
    make_pdf(second, "Second version")
    service = ingestion(sessions, tmp_path)
    original = service.extractor.iter_bytes

    def competing(data, page_numbers):
        with pytest.raises(PdfInputError, match="processing_in_progress"):
            service.ingest("claim", document_id, second)
        yield from original(data, page_numbers)

    service.extractor.iter_bytes = competing
    run_id = service.ingest("claim", document_id, first)
    with sessions() as session:
        assert session.get(DocumentProcessingRun, run_id).status == "completed"
        assert session.get(ClaimDocument, document_id).processing_status == ProcessingStatus.COMPLETED
        assert session.scalar(select(func.count()).select_from(DocumentProcessingRun).where(
            DocumentProcessingRun.claim_document_id == document_id)) == 1


def test_processing_policy_does_not_change_claim_document_status(sessions, tmp_path):
    policy_document_id = make_source(sessions, "policy")
    claim_document_id = make_source(sessions, "claim")
    path = tmp_path / "policy.pdf"
    make_pdf(path)
    ingestion(sessions, tmp_path).ingest("policy", policy_document_id, path)
    with sessions() as session:
        assert session.get(ClaimDocument, claim_document_id).processing_status == ProcessingStatus.PENDING


def test_invalid_input_does_not_change_existing_claim_status(sessions, tmp_path):
    from app.services.pdf_input import PdfInputError

    document_id = make_source(sessions, "claim")
    path = tmp_path / "invalid.pdf"
    path.write_bytes(b"invalid PDF")
    with sessions.begin() as session:
        session.get(ClaimDocument, document_id).processing_status = ProcessingStatus.FAILED
    with pytest.raises(PdfInputError, match="invalid_pdf"):
        ingestion(sessions, tmp_path).ingest("claim", document_id, path)
    with sessions() as session:
        assert session.get(ClaimDocument, document_id).processing_status == ProcessingStatus.FAILED
        assert session.scalar(select(func.count()).select_from(DocumentProcessingRun).where(
            DocumentProcessingRun.claim_document_id == document_id)) == 0


def test_run_and_claim_status_update_atomically(sessions, tmp_path, monkeypatch):
    document_id = make_source(sessions, "claim")
    path = tmp_path / "test.pdf"
    make_pdf(path)
    service = ingestion(sessions, tmp_path)
    original = DocumentRepository._sync_claim

    def fail_completion(session, run, status):
        if status == "completed":
            raise RuntimeError("simulated status write failure")
        original(session, run, status)

    monkeypatch.setattr(DocumentRepository, "_sync_claim", staticmethod(fail_completion))
    with pytest.raises(RuntimeError, match="simulated status write failure"):
        service.ingest("claim", document_id, path)
    with sessions() as session:
        run = session.scalar(select(DocumentProcessingRun).where(DocumentProcessingRun.claim_document_id == document_id))
        assert run.status == "processing"
        assert session.get(ClaimDocument, document_id).processing_status == ProcessingStatus.PROCESSING
