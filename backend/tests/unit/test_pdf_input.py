from contextlib import contextmanager
import hashlib
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest

from app import cli
from app.domain.models import ClaimDocument, PolicyDocument
from app.services.pdf_input import PdfInputError, PdfInputService


class FakeSession:
    def __init__(self, existing):
        self.existing = existing
        self.lookups = []

    def get(self, model, document_id):
        self.lookups.append((model, document_id))
        return object() if (model, document_id) in self.existing else None


def service(tmp_path, model=PolicyDocument, document_id=None, **limits):
    document_id = document_id or uuid4()
    session = FakeSession({(model, document_id)})

    @contextmanager
    def sessions():
        yield session

    return PdfInputService(sessions, tmp_path / "storage", **limits), document_id, session


def pdf_bytes(pages=1, encrypted=False):
    pdf = pymupdf.open()
    for _ in range(pages):
        pdf.new_page().insert_text((72, 72), "Policy P-123")
    options = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "owner_pw": "owner", "user_pw": "secret"} if encrypted else {}
    data = pdf.tobytes(**options)
    pdf.close()
    return data


@pytest.mark.parametrize("source_type,model", [("policy", PolicyDocument), ("claim", ClaimDocument)])
def test_validate_preserves_exact_bytes_and_uses_requested_source(tmp_path, source_type, model):
    svc, document_id, session = service(tmp_path, model)
    data = pdf_bytes(2)
    path = tmp_path / "input.bin"
    path.write_bytes(data)
    first = svc.validate(source_type, document_id, path)
    second = svc.validate(source_type, document_id, path)
    digest = hashlib.sha256(data).hexdigest()
    assert first == second
    assert first.sha256 == digest and first.page_count == 2
    assert (svc.storage_dir / f"{digest}.pdf").read_bytes() == data
    assert len(list(svc.storage_dir.iterdir())) == 1
    assert session.lookups == [(model, document_id)] * 2


@pytest.mark.parametrize("data,code", [(b"", "invalid_pdf"), (b"not pdf", "invalid_pdf"), (b"%PDF-1.7\ncorrupt", "invalid_pdf"), (pdf_bytes(encrypted=True), "encrypted_pdf")])
def test_bad_inputs_do_not_write_storage(tmp_path, data, code):
    svc, document_id, _ = service(tmp_path)
    path = tmp_path / "input.pdf"
    path.write_bytes(data)
    with pytest.raises(PdfInputError, match=code):
        svc.validate("policy", document_id, path)
    assert not svc.storage_dir.exists()


def test_missing_document_checked_before_file(tmp_path):
    svc, _, _ = service(tmp_path)
    with pytest.raises(PdfInputError, match="document_not_found"):
        svc.validate("claim", uuid4(), tmp_path / "missing")
    assert not svc.storage_dir.exists()


def test_missing_file_and_non_regular_file(tmp_path):
    svc, document_id, _ = service(tmp_path)
    for path, code in [(tmp_path / "missing", "file_unreadable"), (tmp_path, "file_not_regular")]:
        with pytest.raises(PdfInputError, match=code):
            svc.validate("policy", document_id, path)


def test_limits(tmp_path):
    path = tmp_path / "input.pdf"
    path.write_bytes(pdf_bytes(2))
    svc, doc_id, _ = service(tmp_path, max_bytes=10)
    with pytest.raises(PdfInputError, match="file_too_large"):
        svc.validate("policy", doc_id, path)
    svc, doc_id, _ = service(tmp_path, max_pages=1)
    with pytest.raises(PdfInputError, match="page_limit_exceeded"):
        svc.validate("policy", doc_id, path)


def test_storage_conflict_does_not_overwrite(tmp_path):
    svc, doc_id, _ = service(tmp_path)
    data = pdf_bytes()
    path = tmp_path / "input.pdf"
    path.write_bytes(data)
    svc.storage_dir.mkdir()
    target = svc.storage_dir / (hashlib.sha256(data).hexdigest() + ".pdf")
    target.write_bytes(b"existing content")
    with pytest.raises(PdfInputError, match="storage_conflict"):
        svc.validate("policy", doc_id, path)
    assert target.read_bytes() == b"existing content"


def test_cli_returns_error_code_without_reading_provider(tmp_path, monkeypatch, capsys):
    svc, document_id, _ = service(tmp_path)
    monkeypatch.setattr(cli, "make_sessions", lambda: svc.sessions)
    monkeypatch.setenv("PDF_STORAGE_DIR", str(svc.storage_dir))
    monkeypatch.setattr(cli, "make_provider", lambda: pytest.fail("provider must not be called"))
    result = cli.main(["validate-pdf", "--source-type", "policy", "--document-id", str(document_id), "--file", str(tmp_path / "missing")])
    assert result == 1
    assert capsys.readouterr().err.strip() == "PDF input error: file_unreadable"


def test_owner_only_encryption_is_rejected(tmp_path):
    svc, document_id, _ = service(tmp_path)
    pdf = pymupdf.open()
    pdf.new_page()
    data = pdf.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner")
    pdf.close()
    path = tmp_path / "encrypted.pdf"
    path.write_bytes(data)
    with pytest.raises(PdfInputError, match="encrypted_pdf"):
        svc.validate("policy", document_id, path)


def test_cli_valid_pdf_reports_hash_and_page_count(tmp_path, monkeypatch, capsys):
    import json

    svc, document_id, _ = service(tmp_path)
    monkeypatch.setattr(cli, "make_sessions", lambda: svc.sessions)
    monkeypatch.setenv("PDF_STORAGE_DIR", str(svc.storage_dir))
    path = tmp_path / "policy.pdf"
    path.write_bytes(pdf_bytes())
    assert cli.main(["validate-pdf", "--source-type", "policy", "--document-id", str(document_id), "--file", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert payload["page_count"] == 1
