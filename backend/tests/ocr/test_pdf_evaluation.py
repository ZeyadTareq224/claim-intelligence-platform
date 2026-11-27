"""Fixture quality and persisted provenance with real English OCR."""
import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from app.document_evaluation import evaluate
from app.domain.models import (DocumentProcessingRun, Organization, Policy, PolicyDocument,
                               PolicyStatus, PolicyType, Vehicle)


def test_labeled_fixtures_and_persisted_provenance(monkeypatch, tmp_path):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url or url == os.environ.get("DATABASE_URL"):
        pytest.fail("TEST_DATABASE_URL must be a separate migrated PostgreSQL database")
    engine = create_engine(url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    org_id, vehicle_id, policy_id, document_id = (uuid4() for _ in range(4))
    try:
        with sessions.begin() as session:
            session.add(Organization(id=org_id, name="Fixture evaluation", code=org_id.hex))
            session.flush()
            session.add(Vehicle(id=vehicle_id, organization_id=org_id, plate_number="TEST", make="Test", model="Test", manufacture_year=2020))
            session.flush()
            session.add(Policy(id=policy_id, organization_id=org_id, vehicle_id=vehicle_id, policy_number="P1",
                policy_type=PolicyType.MOTOR_COMPREHENSIVE, status=PolicyStatus.ACTIVE, effective_date=date(2024, 1, 1),
                expiration_date=date(2025, 1, 1), sum_insured=Decimal("100"), currency="EGP"))
            session.flush()
            session.add(PolicyDocument(id=document_id, policy_id=policy_id, filename="evaluation.pdf", content_type="application/pdf", storage_key="synthetic", document_version=1))
        monkeypatch.setenv("PDF_STORAGE_DIR", str(tmp_path / "storage"))
        for name, value in {"PDF_OCR_LANGUAGE": "eng", "PDF_OCR_DPI": "200", "PDF_OCR_TIMEOUT_SECONDS": "60",
                            "PDF_OCR_MAX_PIXELS": "25000000", "PDF_NATIVE_TIMEOUT_SECONDS": "30",
                            "PDF_MAX_BYTES": "26214400", "PDF_MAX_PAGES": "200"}.items():
            monkeypatch.setenv(name, value)
        first = evaluate(sessions, document_id)
        assert first["passed"], first
        assert first["totals"]["native_covered"] == first["totals"]["native_expected"] == 2
        assert first["totals"]["ocr_fallback_pages"] == 2
        assert first["totals"]["provenance_correct"] == first["totals"]["pages"] == 5
        second = evaluate(sessions, document_id)
        assert second["passed"] and first["totals"] == second["totals"]
        assert [case.get("run_id") for case in first["cases"]] == [case.get("run_id") for case in second["cases"]]
    finally:
        with sessions.begin() as session:
            session.execute(delete(DocumentProcessingRun).where(DocumentProcessingRun.policy_document_id == document_id))
            session.execute(delete(PolicyDocument).where(PolicyDocument.id == document_id))
            session.execute(delete(Policy).where(Policy.id == policy_id))
            session.execute(delete(Vehicle).where(Vehicle.id == vehicle_id))
            session.execute(delete(Organization).where(Organization.id == org_id))
        engine.dispose()
