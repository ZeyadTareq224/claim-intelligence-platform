"""Exercise persistence against a separate migrated PostgreSQL database."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.ai.extraction import ClaimExtraction
from app.ai.providers.base import ProviderError, ProviderResult
from app.domain.models import AnalysisStatus, Claim, ClaimAnalysis, ClaimStatus, Organization
from app.repositories.claims import ClaimRepository, ClaimNotFound
from app.services.claim_service import ClaimExtractionService


class FakeProvider:
    provider_name = "fake"
    model_name = "fake-model"
    prompt_version = "v1"
    failure = None

    def extract(self, text):
        if self.failure:
            raise ProviderError(self.failure)
        return ProviderResult(ClaimExtraction(incident_type="collision", incident_date=None, policy_number=None,
                                             damaged_parts=[], estimated_repair_cost=None, currency=None, uncertainties=[]), 12, 5)


@pytest.fixture
def isolated_claim():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to an isolated migrated PostgreSQL database")
    if url == os.environ.get("DATABASE_URL"):
        pytest.fail("TEST_DATABASE_URL must differ from DATABASE_URL")
    engine = create_engine(url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    org_id, claim_id = uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(Organization(id=org_id, name="Test", code=str(uuid4())))
        session.flush()
        session.add(Claim(id=claim_id, organization_id=org_id, policy_id=None, claim_number="C1", description="A collision", status=ClaimStatus.SUBMITTED))
    yield sessions, claim_id
    with sessions.begin() as session:
        session.execute(delete(ClaimAnalysis).where(ClaimAnalysis.claim_id == claim_id))
        session.execute(delete(Claim).where(Claim.id == claim_id))
        session.execute(delete(Organization).where(Organization.id == org_id))
    engine.dispose()


def test_success_reanalysis_and_failure_preserve_claim(isolated_claim):
    sessions, claim_id = isolated_claim
    provider = FakeProvider()
    service = ClaimExtractionService(provider, ClaimRepository(sessions))
    first = service.extract_claim(claim_id)
    second = service.extract_claim(claim_id)
    provider.failure = "timeout"
    failed = service.extract_claim(claim_id)
    assert first.id != second.id != failed.id
    assert first.status == AnalysisStatus.COMPLETED and first.extracted_data["incident_type"] == "collision"
    assert first.missing_fields == ["incident_date", "policy_number"]
    assert first.input_tokens == 12 and first.output_tokens == 5
    assert failed.status == AnalysisStatus.FAILED and failed.extracted_data is None and failed.error_code == "timeout"
    assert len(first.input_fingerprint) == 64 and first.input_fingerprint == failed.input_fingerprint
    with sessions() as session:
        assert len(session.scalars(select(ClaimAnalysis).where(ClaimAnalysis.claim_id == claim_id)).all()) == 3
        assert session.get(Claim, claim_id).description == "A collision"


def test_missing_claim_does_not_call_provider(isolated_claim):
    sessions, _ = isolated_claim
    class NeverCalled(FakeProvider):
        def extract(self, text):
            raise AssertionError("provider must not be called")
    with pytest.raises(ClaimNotFound):
        ClaimExtractionService(NeverCalled(), ClaimRepository(sessions)).extract_claim(uuid4())


def test_claim_changes_during_provider_call_are_not_overwritten(isolated_claim):
    import hashlib

    sessions, claim_id = isolated_claim

    class ConcurrentEdit(FakeProvider):
        def extract(self, text):
            with sessions.begin() as session:
                session.get(Claim, claim_id).description = "Updated while processing"
            return super().extract(text)

    result = ClaimExtractionService(ConcurrentEdit(), ClaimRepository(sessions)).extract_claim(claim_id)
    assert result.input_fingerprint == hashlib.sha256(b"A collision").hexdigest()
    with sessions() as session:
        assert session.get(Claim, claim_id).description == "Updated while processing"


def test_cli_claim_command_saves_analysis(isolated_claim, monkeypatch, capsys):
    import json
    from app import cli

    sessions, claim_id = isolated_claim
    monkeypatch.setattr(cli, "make_provider", lambda: FakeProvider())
    monkeypatch.setattr(cli, "make_sessions", lambda: sessions)
    assert cli.main(["extract", "--claim-id", str(claim_id)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed"
    with sessions() as session:
        assert str(session.scalar(select(ClaimAnalysis.id).where(ClaimAnalysis.claim_id == claim_id))) == output["analysis_id"]


def test_gemini_attempt_persists_provider_metadata(isolated_claim):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock
    from google.genai import types
    from app.ai.providers.gemini_provider import GeminiExtractionProvider

    sessions, claim_id = isolated_claim
    facts = {"incident_type": "collision", "incident_date": None, "policy_number": None, "damaged_parts": [],
             "estimated_repair_cost": None, "currency": None, "uncertainties": []}
    response = SimpleNamespace(candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)],
                               text=json.dumps(facts), usage_metadata=SimpleNamespace(prompt_token_count=7,
                               candidates_token_count=3, thoughts_token_count=2))
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(return_value=response)))
    provider = GeminiExtractionProvider(model_name="gemini-test", api_key="not-a-key", client=client)
    analysis = ClaimExtractionService(provider, ClaimRepository(sessions)).extract_claim(claim_id)
    assert analysis.provider == "gemini" and analysis.model_name == "gemini-test"
    assert analysis.input_tokens == 7 and analysis.output_tokens == 5
    assert analysis.status == AnalysisStatus.COMPLETED and analysis.extracted_data["incident_type"] == "collision"
