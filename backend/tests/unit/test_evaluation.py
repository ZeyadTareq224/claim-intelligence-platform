import copy
import json

import pytest

from app.ai.evaluation.runner import DATASET, evaluate, markdown, normalize, score_case, validate_dataset
from app.ai.extraction import ClaimExtraction
from app.ai.providers.base import ProviderError, ProviderResult


def test_dataset_labels_are_valid_and_scenarios_cover_requirements():
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    validate_dataset(dataset)
    assert len(dataset["cases"]) >= 10
    assert any("injection" in case["id"] for case in dataset["cases"])
    assert any("arabic" in case["id"] for case in dataset["cases"])
    assert len({case["uncertainty_topic"] for case in dataset["cases"] if case["uncertainty_topic"]}) >= 2


def test_normalization_and_unsupported_candidates():
    assert normalize("damaged_parts", ["Bonnet", "المصد الأمامي"]) == ["front bumper", "hood"]
    case = json.loads(DATASET.read_text(encoding="utf-8"))["cases"][0]
    actual = copy.deepcopy(case["expected"])
    actual.update(damaged_parts=["front bumper", "door"], estimated_repair_cost="12000.00")
    result = score_case(case, ClaimExtraction.model_validate(actual), [])
    assert result["field_matches"]["estimated_repair_cost"]
    assert not result["field_matches"]["damaged_parts"]
    assert result["unsupported_candidates"] == [{"field": "damaged_parts", "values": ["door"]}]


def test_uncertainty_requires_the_missing_fact_and_relevant_topic():
    case = json.loads(DATASET.read_text(encoding="utf-8"))["cases"][5]
    actual = copy.deepcopy(case["expected"])
    actual["uncertainties"] = ["The reported dates conflict"]
    assert score_case(case, ClaimExtraction.model_validate(actual), ["incident_date"])["uncertainty_handled"]
    actual["uncertainties"] = ["The color is uncertain"]
    assert not score_case(case, ClaimExtraction.model_validate(actual), ["incident_date"])["uncertainty_handled"]
    actual["incident_date"] = "2024-08-01"
    actual["uncertainties"] = ["The date is uncertain"]
    assert not score_case(case, ClaimExtraction.model_validate(actual), [])["uncertainty_handled"]


def test_failure_counts_separately_and_uses_only_successes_for_field_denominators():
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    dataset["cases"] = dataset["cases"][:2]
    class FakeProvider:
        provider_name = "fake"
        model_name = "fake-model"
        prompt_version = "v1"
        def __init__(self): self.calls = 0
        def extract(self, text):
            self.calls += 1
            if self.calls == 2: raise ProviderError("rate_limit")
            return ProviderResult(ClaimExtraction.model_validate(dataset["cases"][0]["expected"]), 8, 4)
    result = evaluate(dataset, FakeProvider())
    assert result["total"] == 2 and result["schema_valid"] == 1
    assert result["provider_errors"] == {"rate_limit": 1}
    assert result["fields"]["incident_type"] == {"correct": 1, "evaluated": 1}
    assert result["input_tokens"] == 8 and result["input_usage_available"] == 1
    assert "rate_limit" in markdown(result)


def test_invalid_labels_rejected_before_provider_calls():
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    dataset["cases"][0]["missing_fields"] = ["incident_date"]
    with pytest.raises(ValueError, match="inconsistent"):
        validate_dataset(dataset)
