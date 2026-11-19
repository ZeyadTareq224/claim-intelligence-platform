import copy
import json

import pytest

from app.ai.evaluation.label_review import dataset_fingerprint, is_reviewed
from app.ai.evaluation.review import apply_review, review_template
from app.ai.evaluation.runner import DATASET, evaluate, main, markdown, normalize, score_case, validate_dataset
from app.ai.extraction import ClaimExtraction
from app.ai.providers.base import ProviderError, ProviderResult


@pytest.fixture
def dataset():
    return json.loads(DATASET.read_text(encoding="utf-8"))


def test_expanded_dataset_has_valid_source_excerpts_and_language_coverage(dataset):
    validate_dataset(dataset)
    assert len(dataset["cases"]) == 40
    assert sum(case["language"] == "en" for case in dataset["cases"]) == 20
    assert sum(case["language"] in ("ar", "mixed") for case in dataset["cases"]) == 20
    assert any("injection" in case["scenario"].lower() for case in dataset["cases"])
    assert len({case["uncertainty_topic"] for case in dataset["cases"] if case["uncertainty_topic"]}) >= 5
    assert {case["expected"]["incident_type"] for case in dataset["cases"]} >= {"collision", "theft", "fire", "weather_damage", "other"}
    assert dataset["label_review"]["status"] in {"pending", "reviewed"}
    if dataset["label_review"]["status"] == "reviewed":
        assert is_reviewed(dataset)


def test_mismatched_fact_is_queued_for_review_not_automatically_unsupported(dataset):
    assert normalize("damaged_parts", ["Bonnet", "المصد الأمامي"]) == ["front bumper", "hood"]
    case = dataset["cases"][0]
    actual = copy.deepcopy(case["expected"])
    actual.update(damaged_parts=["front bumper", "door"], estimated_repair_cost="4500.00")
    result = score_case(case, ClaimExtraction.model_validate(actual), [])
    assert result["field_matches"]["estimated_repair_cost"]
    assert not result["field_matches"]["damaged_parts"]
    assert "unsupported_candidates" not in result
    fact = next(item for item in result["support_review"] if item["value"] == "door")
    assert fact["verdict"] is None


def test_ambiguous_date_selection_is_wrong_not_automatically_hallucinated(dataset):
    case = dataset["cases"][5]
    actual = copy.deepcopy(case["expected"])
    actual.update(incident_date="2023-06-09", uncertainties=["The date could also be 2023-06-10"])
    result = score_case(case, ClaimExtraction.model_validate(actual), [])
    assert not result["field_matches"]["incident_date"] and not result["uncertainty_handled"]
    fact = next(item for item in result["support_review"] if item["field"] == "incident_date")
    assert fact["verdict"] is None and fact["value"] in case["description"]


def test_uncertainty_requires_unknown_field_and_relevant_topic(dataset):
    case = dataset["cases"][5]
    actual = copy.deepcopy(case["expected"])
    actual["uncertainties"] = ["The reported dates conflict"]
    assert score_case(case, ClaimExtraction.model_validate(actual), ["incident_date"])["uncertainty_handled"]
    actual["uncertainties"] = ["The color is uncertain"]
    assert not score_case(case, ClaimExtraction.model_validate(actual), ["incident_date"])["uncertainty_handled"]


def test_provider_errors_and_unreviewed_facts_are_separate(dataset):
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
    assert result["schema_valid"] == 1 and result["provider_errors"] == {"rate_limit": 1}
    assert result["fields"]["incident_type"] == {"correct": 1, "evaluated": 1}
    assert result["support_summary"]["reviewed"] == 0 and result["support_summary"]["unreviewed"] > 0
    assert "Unsupported-fact rate is not measured" in markdown(result)


def test_evidence_and_label_review_are_bound_to_dataset(dataset):
    dataset["cases"][0]["source_evidence"]["incident_type"] = ["fabricated excerpt"]
    with pytest.raises(ValueError, match="evidence not present"):
        validate_dataset(dataset)
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    dataset["label_review"] = {"status": "reviewed", "reviewer": "human", "reviewed_at": "2026-11-20T00:00:00+00:00",
                               "dataset_sha256": dataset_fingerprint(dataset)}
    assert is_reviewed(dataset)
    dataset["cases"][0]["description"] += " changed"
    assert not is_reviewed(dataset)


def test_live_run_rejects_unreviewed_labels_before_provider_initialization(monkeypatch, capsys, tmp_path, dataset):
    dataset["label_review"] = {"status": "pending"}
    path = tmp_path / "unreviewed.json"
    path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("app.ai.evaluation.runner.make_provider", lambda: pytest.fail("provider must not be initialized"))
    with pytest.raises(SystemExit):
        main(["--dataset", str(path)])
    assert "independent human review" in capsys.readouterr().err


def test_support_review_requires_actual_evidence_and_preserves_mismatch(dataset):
    dataset["cases"] = dataset["cases"][:1]
    class FakeProvider:
        provider_name, model_name, prompt_version = "fake", "fake", "v1"
        def extract(self, text):
            data = copy.deepcopy(dataset["cases"][0]["expected"])
            data["incident_date"] = "2023-01-05"
            data["uncertainties"] = ["I cannot verify the driver"]
            return ProviderResult(ClaimExtraction.model_validate(data))
    result = evaluate(dataset, FakeProvider())
    review = review_template(result)
    review["attestation"] = {"reviewer": "reviewer", "independent_human": True}
    date_fact = next(item for item in review["cases"][0]["facts"] if item["field"] == "incident_date")
    date_fact.update(verdict="unsupported", reviewer="reviewer", rationale="The claimed date is absent", evidence=[])
    reviewed = apply_review(result, review)
    assert not reviewed["cases"][0]["field_matches"]["incident_date"]
    assert reviewed["support_summary"]["unsupported"] == 1
    assert reviewed["support_summary"]["unreviewed"] > 0
    assert any(item["field"] == "uncertainties" for item in reviewed["cases"][0]["support_review"])
    date_fact.update(verdict="supported", evidence=["invented quote"])
    with pytest.raises(ValueError, match="exact excerpts"):
        apply_review(result, review)
    review["run_id"] = "other-run"
    with pytest.raises(ValueError, match="does not belong"):
        apply_review(result, review)


def test_human_review_attestation_and_stale_label_invalidation(dataset, tmp_path):
    from app.ai.evaluation.label_review import main as record_review
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        record_review(["--dataset", str(path), "--reviewer", "reviewer"])
    assert record_review(["--dataset", str(path), "--reviewer", "reviewer",
                          "--confirm-independent-human-review"]) == 0
    reviewed = json.loads(path.read_text(encoding="utf-8"))
    assert is_reviewed(reviewed)
    assert reviewed["label_review"]["reviewer"] == "reviewer"
    reviewed["cases"][0]["expected"]["policy_number"] = "changed"
    assert not is_reviewed(reviewed)


def test_ambiguous_text_date_review_is_not_forced_to_unsupported(dataset):
    dataset["cases"] = [dataset["cases"][5]]
    class FakeProvider:
        provider_name, model_name, prompt_version = "fake", "fake", "v1"
        def extract(self, text):
            data = copy.deepcopy(dataset["cases"][0]["expected"])
            data["incident_date"] = "2023-06-09"
            return ProviderResult(ClaimExtraction.model_validate(data))
    report = evaluate(dataset, FakeProvider())
    template = review_template(report)
    template["attestation"] = {"reviewer": "reviewer", "independent_human": True}
    fact = next(item for item in template["cases"][0]["facts"] if item["field"] == "incident_date")
    fact.update(verdict="unclear", reviewer="reviewer", rationale="One of two possible dates; not established as the incident date.",
                evidence=["either 2023-06-09 or 2023-06-10"])
    result = apply_review(report, template)
    assert result["cases"][0]["field_matches"]["incident_date"] is False
    assert result["support_summary"]["unclear"] == 1 and result["support_summary"]["unsupported"] == 0
    assert report["support_summary"]["reviewed"] == 0  # Template changes do not mutate the original report.


def test_support_review_rejects_empty_evidence_for_supported_fact(dataset):
    dataset["cases"] = dataset["cases"][:1]
    class FakeProvider:
        provider_name, model_name, prompt_version = "fake", "fake", "v1"
        def extract(self, text):
            return ProviderResult(ClaimExtraction.model_validate(dataset["cases"][0]["expected"]))
    report = evaluate(dataset, FakeProvider())
    template = review_template(report)
    template["attestation"] = {"reviewer": "reviewer", "independent_human": True}
    template["cases"][0]["facts"][0].update(verdict="supported", reviewer="reviewer", rationale="Relevant", evidence=[])
    with pytest.raises(ValueError, match="need at least one exact source excerpt"):
        apply_review(report, template)


def test_stale_attestation_is_not_reported_as_verified(dataset):
    dataset["cases"] = dataset["cases"][:1]
    dataset["label_review"] = {"status": "reviewed", "reviewer": "reviewer", "reviewed_at": "2026-11-20T00:00:00+00:00",
                               "dataset_sha256": "stale"}
    class FakeProvider:
        provider_name, model_name, prompt_version = "fake", "fake", "v1"
        def extract(self, text):
            return ProviderResult(ClaimExtraction.model_validate(dataset["cases"][0]["expected"]))
    result = evaluate(dataset, FakeProvider())
    assert not result["label_review_verified"]
    assert "not independently human-reviewed" in markdown(result)


def test_support_verdict_requires_independent_human_self_attestation(dataset):
    dataset["cases"] = dataset["cases"][:1]
    class FakeProvider:
        provider_name, model_name, prompt_version = "fake", "fake", "v1"
        def extract(self, text):
            return ProviderResult(ClaimExtraction.model_validate(dataset["cases"][0]["expected"]))
    report = evaluate(dataset, FakeProvider())
    template = review_template(report)
    template["cases"][0]["facts"][0].update(verdict="unsupported", reviewer="someone", rationale="Not established", evidence=[])
    with pytest.raises(ValueError, match="independent-human self-attestation"):
        apply_review(report, template)


def test_support_review_rejects_changed_model_output(dataset):
    dataset["cases"] = dataset["cases"][:1]
    class FakeProvider:
        provider_name, model_name, prompt_version = "fake", "fake", "v1"
        def extract(self, text):
            return ProviderResult(ClaimExtraction.model_validate(dataset["cases"][0]["expected"]))
    report = evaluate(dataset, FakeProvider())
    template = review_template(report)
    report["cases"][0]["actual"]["policy_number"] = "different"
    with pytest.raises(ValueError, match="does not belong"):
        apply_review(report, template)


def test_support_review_rejects_facts_changed_without_changing_output(dataset):
    dataset["cases"] = dataset["cases"][:1]
    class FakeProvider:
        provider_name, model_name, prompt_version = "fake", "fake", "v1"
        def extract(self, text):
            return ProviderResult(ClaimExtraction.model_validate(dataset["cases"][0]["expected"]))
    report = evaluate(dataset, FakeProvider())
    template = review_template(report)
    report["cases"][0]["support_review"][0]["value"] = "tampered"
    with pytest.raises(ValueError, match="facts do not match"):
        apply_review(report, template)


def test_all_expanded_cases_run_with_a_fake_provider_without_inventing_reviews(dataset):
    cases = {case["description"]: case for case in dataset["cases"]}
    class FakeProvider:
        provider_name, model_name, prompt_version = "fake", "fake", "v1"
        def extract(self, text):
            return ProviderResult(ClaimExtraction.model_validate(cases[text]["expected"]))
    report = evaluate(dataset, FakeProvider())
    assert report["schema_valid"] == 40
    assert all(result["correct"] == 40 for result in report["fields"].values())
    assert report["missing_correct"] == 40
    assert report["uncertainty_correct"] == report["uncertainty_evaluated"]
    assert report["support_summary"]["reviewed"] == 0
    assert report["support_summary"]["unreviewed"] > 0


def test_support_review_cli_writes_new_files_without_modifying_original(dataset, tmp_path):
    from app.ai.evaluation.review import main as review_main
    dataset["cases"] = dataset["cases"][:1]
    class FakeProvider:
        provider_name, model_name, prompt_version = "fake", "fake", "v1"
        def extract(self, text):
            return ProviderResult(ClaimExtraction.model_validate(dataset["cases"][0]["expected"]))
    result = evaluate(dataset, FakeProvider())
    source = tmp_path / "result.json"
    template = tmp_path / "review.json"
    output = tmp_path / "reviewed.json"
    report = tmp_path / "reviewed.md"
    source.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    original = source.read_bytes()
    assert review_main(["--results", str(source), "--template", str(template)]) == 0
    data = json.loads(template.read_text(encoding="utf-8"))
    data["attestation"] = {"reviewer": "human", "independent_human": True}
    fact = data["cases"][0]["facts"][0]
    fact.update(verdict="supported", reviewer="human", rationale="The claim describes this incident",
                evidence=[dataset["cases"][0]["source_evidence"]["incident_type"][0]])
    template.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert review_main(["--results", str(source), "--apply", str(template), "--output", str(output), "--report", str(report)]) == 0
    assert source.read_bytes() == original
    assert json.loads(output.read_text(encoding="utf-8"))["support_summary"]["supported"] == 1
    assert "1 supported" in report.read_text(encoding="utf-8")
