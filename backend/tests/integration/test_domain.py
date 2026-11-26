"""Requires an explicitly configured, isolated PostgreSQL test database."""
import os
from datetime import date
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.domain.models import Organization, Vehicle, Policy, Claim, ClaimAnalysis, PolicyType, PolicyStatus, ClaimStatus, AnalysisStatus


@pytest.fixture
def session():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to an isolated migrated PostgreSQL database")
    if url == os.environ.get("DATABASE_URL"):
        pytest.fail("TEST_DATABASE_URL must differ from DATABASE_URL")
    engine = create_engine(url)
    connection = engine.connect()
    transaction = connection.begin()
    with Session(bind=connection, join_transaction_mode="create_savepoint") as s:
        yield s
    transaction.rollback()
    connection.close()
    engine.dispose()


def setup_policy(s):
    org = Organization(id=uuid4(), name="Test", code=str(uuid4()))
    vehicle = Vehicle(id=uuid4(), organization_id=org.id, vin=None, plate_number="TEST", make="Example", model="A", manufacture_year=2020)
    s.add(org)
    s.flush()
    s.add(vehicle)
    s.flush()
    policy = Policy(id=uuid4(), organization_id=org.id, vehicle_id=vehicle.id, policy_number="P1", policy_type=PolicyType.MOTOR_COMPREHENSIVE, status=PolicyStatus.ACTIVE, effective_date=date(2024, 1, 1), expiration_date=date(2025, 1, 1), sum_insured=Decimal("200.00"), currency="EGP")
    return org, vehicle, policy


def test_valid_and_multiple_analyses(session):
    org, _, policy = setup_policy(session)
    session.add(policy)
    session.flush()
    claim = Claim(id=uuid4(), organization_id=org.id, policy_id=policy.id, claim_number="C1", description="A collision", status=ClaimStatus.SUBMITTED)
    session.add(claim)
    session.flush()
    common = dict(claim_id=claim.id, missing_fields=[], uncertainties=[], provider="fake", model_name="fake", prompt_version="1")
    session.add_all([ClaimAnalysis(status=AnalysisStatus.COMPLETED, extracted_data={"incident_type": "collision"}, **common), ClaimAnalysis(status=AnalysisStatus.FAILED, extracted_data=None, error_code="timeout", **common)])
    session.flush()
    assert len(claim.analyses) == 2
    assert claim.description == "A collision"


@pytest.mark.parametrize("change", ["dates", "amount", "wrong_vehicle", "duplicate_number"])
def test_policy_constraints(session, change):
    org, vehicle, policy = setup_policy(session)
    if change == "dates":
        policy.expiration_date = policy.effective_date
    elif change == "amount":
        policy.sum_insured = Decimal("-1")
    elif change == "wrong_vehicle":
        other = Organization(id=uuid4(), name="Other", code=str(uuid4()))
        session.add(other)
        session.flush()
        vehicle.organization_id = other.id
        session.flush()
    elif change == "duplicate_number":
        session.add(policy)
        session.flush()
        policy = Policy(id=uuid4(), organization_id=org.id, vehicle_id=vehicle.id, policy_number="P1", policy_type=PolicyType.MOTOR_COMPREHENSIVE, status=PolicyStatus.ACTIVE, effective_date=date(2024, 1, 1), expiration_date=date(2025, 1, 1), sum_insured=Decimal("200"), currency="EGP")
    session.add(policy)
    with pytest.raises(IntegrityError):
        session.flush()


def test_unlinked_claim_and_cross_tenant_policy(session):
    org, _, policy = setup_policy(session)
    session.add(policy)
    other = Organization(id=uuid4(), name="Other", code=str(uuid4()))
    session.add(other)
    session.flush()
    session.add(Claim(id=uuid4(), organization_id=other.id, policy_id=None, claim_number="C1", description="Unknown policy", status=ClaimStatus.DRAFT))
    session.flush()
    session.add(Claim(id=uuid4(), organization_id=other.id, policy_id=policy.id, claim_number="C2", description="Wrong tenant", status=ClaimStatus.DRAFT))
    with pytest.raises(IntegrityError):
        session.flush()


def test_whitespace_claim_is_rejected(session):
    org, _, _ = setup_policy(session)
    session.add(Claim(id=uuid4(), organization_id=org.id, policy_id=None, claim_number="BLANK", description="\t\n", status=ClaimStatus.DRAFT))
    with pytest.raises(IntegrityError):
        session.flush()


def test_same_policy_number_in_different_organizations(session):
    _, _, first = setup_policy(session)
    _, _, second = setup_policy(session)
    session.add_all([first, second])
    session.flush()
    assert first.policy_number == second.policy_number


def test_invalid_foreign_key(session):
    session.add(Claim(id=uuid4(), organization_id=uuid4(), policy_id=None, claim_number="MISSING", description="Missing organization", status=ClaimStatus.DRAFT))
    with pytest.raises(IntegrityError):
        session.flush()


def test_pdf_input_requires_correct_existing_document_type(session, tmp_path):
    from contextlib import nullcontext
    import pymupdf
    from app.domain.models import PolicyDocument
    from app.services.pdf_input import PdfInputError, PdfInputService

    _, _, policy = setup_policy(session)
    session.add(policy)
    session.flush()
    document = PolicyDocument(policy_id=policy.id, filename="policy.pdf", content_type="application/pdf", storage_key="fixture", document_version=1)
    session.add(document)
    session.flush()
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Policy P-123")
    path = tmp_path / "policy.pdf"
    path.write_bytes(pdf.tobytes())
    pdf.close()
    service = PdfInputService(lambda: nullcontext(session), tmp_path / "storage")
    with pytest.raises(PdfInputError, match="document_not_found"):
        service.validate("claim", document.id, path)
    result = service.validate("policy", document.id, path)
    assert result.document_id == document.id and result.page_count == 1
    assert (service.storage_dir / f"{result.sha256}.pdf").read_bytes() == path.read_bytes()
