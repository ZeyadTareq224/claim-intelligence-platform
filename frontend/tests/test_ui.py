"""Smoke-test the actual Streamlit CRUD flow against a disposable migrated database."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from streamlit.testing.v1 import AppTest


@pytest.fixture
def ui(monkeypatch):
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("set TEST_DATABASE_URL to a separate migrated PostgreSQL database")
    if test_url == os.environ.get("DATABASE_URL"):
        pytest.fail("TEST_DATABASE_URL must differ from DATABASE_URL")
    monkeypatch.setenv("DATABASE_URL", test_url)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "main.py"), default_timeout=20).run()
    assert not app.exception
    return app


def test_organization_crud_and_all_screens(ui):
    code = f"UI{uuid4().hex[:12]}"
    ui.text_input(key="create_Organizations_name").set_value("UI Test")
    ui.text_input(key="create_Organizations_code").set_value(code)
    ui.button[0].click().run()
    assert not ui.exception and ui.success

    # Select the record explicitly; other test data may be present.
    ui.selectbox(key="select_Organizations").select_index(next(
        i for i, label in enumerate(ui.selectbox(key="select_Organizations").options) if code in label
    )).run()
    edit = next(x for x in ui.text_input if x.key.startswith("edit_") and x.key.endswith("_name"))
    edit.set_value("Updated UI Test")
    next(x for x in ui.button if x.label == "Save changes").click().run()
    assert not ui.exception and ui.success

    next(x for x in ui.checkbox if x.key.startswith("confirm_")).check().run()
    next(x for x in ui.button if x.label == "Delete").click().run()
    assert not ui.exception and ui.success

    for name in ("Vehicles", "Policies", "Policy clauses", "Claims", "Claim documents", "Policy documents", "Claim analyses", "PDF upload"):
        ui.radio[0].set_value(name).run()
        assert not ui.exception and not ui.error, name


def test_pdf_upload_validates_and_preserves(ui, monkeypatch, tmp_path):
    from datetime import date
    from decimal import Decimal
    from types import SimpleNamespace

    import pymupdf
    import streamlit as st
    from sqlalchemy import create_engine, delete
    from sqlalchemy.orm import Session
    from app.domain.models import Organization, Vehicle, Policy, PolicyDocument, PolicyType, PolicyStatus, DocumentProcessingRun

    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    org_id, vehicle_id, policy_id, document_id = (uuid4() for _ in range(4))
    filename = f"upload-{document_id}.pdf"
    with Session(engine) as session:
        session.add(Organization(id=org_id, name="Upload test", code=org_id.hex))
        session.flush()
        session.add(Vehicle(id=vehicle_id, organization_id=org_id, plate_number="TEST", make="Test", model="Test", manufacture_year=2020))
        session.flush()
        session.add(Policy(id=policy_id, organization_id=org_id, vehicle_id=vehicle_id, policy_number="P1", policy_type=PolicyType.MOTOR_COMPREHENSIVE, status=PolicyStatus.ACTIVE, effective_date=date(2024, 1, 1), expiration_date=date(2025, 1, 1), sum_insured=Decimal("100"), currency="EGP"))
        session.flush()
        session.add(PolicyDocument(id=document_id, policy_id=policy_id, filename=filename, content_type="application/pdf", storage_key="test", document_version=1))
        session.commit()
    try:
        pdf = pymupdf.open()
        pdf.new_page().insert_text((72, 72), "Synthetic policy")
        data = pdf.tobytes()
        pdf.close()
        monkeypatch.setenv("PDF_STORAGE_DIR", str(tmp_path))
        monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: SimpleNamespace(size=len(data), getvalue=lambda: data))
        ui.radio[0].set_value("PDF upload").run()
        document = next(item for item in ui.selectbox if item.label == "Existing document")
        assert any(filename in label for label in document.options)
        document.set_value(document_id).run()
        next(item for item in ui.button if item.label == "Process PDF").click().run()
        assert not ui.exception and not ui.error
        assert any("PDF processed" in item.value for item in ui.success)
        assert [path.read_bytes() for path in tmp_path.iterdir()] == [data]
        from sqlalchemy import func, select
        with Session(engine) as session:
            run = session.scalar(select(DocumentProcessingRun).where(DocumentProcessingRun.policy_document_id == document_id))
            assert run.status == "completed" and run.pages[0].blocks[0].raw_text.strip() == "Synthetic policy"
            run_id = run.id
        next(item for item in ui.button if item.label == "Process PDF").click().run()
        assert not ui.exception and not ui.error
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(DocumentProcessingRun).where(DocumentProcessingRun.policy_document_id == document_id)) == 1
            assert session.get(DocumentProcessingRun, run_id) is not None

        from app.services.pdf_extraction import OcrError, TesseractOcr
        source = pymupdf.open()
        source_page = source.new_page(width=300, height=100)
        source_page.insert_text((20, 50), "Scan needs OCR", fontsize=20)
        image = source_page.get_pixmap().tobytes("png")
        mixed = pymupdf.open()
        mixed.new_page().insert_text((72, 72), "Digital page")
        scanned = mixed.new_page(width=300, height=100)
        scanned.insert_image(scanned.rect, stream=image)
        mixed_data = mixed.tobytes()
        mixed.close()
        source.close()
        monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: SimpleNamespace(size=len(mixed_data), getvalue=lambda: mixed_data))
        def fail_ocr(self, image):
            raise OcrError("ocr_timeout")
        monkeypatch.setattr(TesseractOcr, "recognize", fail_ocr)
        next(item for item in ui.button if item.label == "Process PDF").click().run()
        assert not ui.exception
        assert any("partially processed" in item.value for item in ui.warning)
        assert any("ocr_timeout" in item.value for item in ui.error)
    finally:
        with Session(engine) as session:
            session.execute(delete(DocumentProcessingRun).where(DocumentProcessingRun.policy_document_id == document_id))
            for model, ident in ((PolicyDocument, document_id), (Policy, policy_id), (Vehicle, vehicle_id), (Organization, org_id)):
                session.execute(delete(model).where(model.id == ident))
            session.commit()
        engine.dispose()
