from uuid import uuid4

import pytest

from app.ai.extraction import ClaimExtraction
from app.ai.providers.base import ProviderError, ProviderResult
from app.services.claim_service import ClaimExtractionService


class FakeProvider:
    provider_name = "fake"
    model_name = "test"
    prompt_version = "test-v1"

    def extract(self, text):
        return ProviderResult(ClaimExtraction(incident_type="collision", incident_date=None, policy_number=None,
                                             damaged_parts=[], estimated_repair_cost=None, currency=None,
                                             uncertainties=["Conflicting dates"]), 4, 2)


def test_ad_hoc_result_separates_missing_and_uncertainties():
    result = ClaimExtractionService(FakeProvider()).extract_text("Collision, conflicting dates")
    assert result.missing_fields == ["incident_date", "policy_number"]
    assert result.extracted_data.uncertainties == ["Conflicting dates"]
    assert result.input_tokens == 4 and result.output_tokens == 2 and result.latency_ms >= 0


@pytest.mark.parametrize("text", ["", " \n", "x" * 20001])
def test_invalid_input_never_calls_provider(text):
    class NeverCalled(FakeProvider):
        def extract(self, text):
            raise AssertionError("provider must not be called")
    with pytest.raises(ValueError):
        ClaimExtractionService(NeverCalled()).extract_text(text)


def test_ad_hoc_provider_failure_surfaces_safe_code():
    class Broken(FakeProvider):
        def extract(self, text):
            raise ProviderError("timeout")
    with pytest.raises(ProviderError, match="timeout"):
        ClaimExtractionService(Broken()).extract_text("some claim")


def test_claim_requires_repository():
    with pytest.raises(ValueError, match="repository"):
        ClaimExtractionService(FakeProvider()).extract_claim(uuid4())
