import json
from uuid import uuid4

import pytest

from app import cli
from app.ai.extraction import ClaimExtraction
from app.ai.providers.base import ProviderError, ProviderResult


@pytest.fixture(autouse=True)
def isolate_provider_configuration(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai")
    for name in ("OPENAI_API_KEY", "OPENAI_MODEL", "GEMINI_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)


class FakeProvider:
    provider_name = "fake"
    model_name = "mock"
    prompt_version = "v1"
    failure = False

    def __init__(self, **kwargs):
        pass

    def extract(self, text):
        if self.failure:
            raise ProviderError("authentication")
        return ProviderResult(ClaimExtraction(incident_type="theft", incident_date=None, policy_number=None,
                                             damaged_parts=[], estimated_repair_cost=None, currency=None, uncertainties=[]))


def test_ad_hoc_file_does_not_require_database(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("app.ai.providers.config.OpenAIExtractionProvider", FakeProvider)
    monkeypatch.setattr(cli, "make_sessions", lambda: (_ for _ in ()).throw(AssertionError("no database needed")))
    path = tmp_path / "claim.txt"
    path.write_text("A vehicle was stolen", encoding="utf-8")
    assert cli.main(["extract", "--text-file", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["extracted_data"]["incident_type"] == "theft"
    assert result["missing_fields"] == ["incident_date", "policy_number"]
    assert "analysis_id" not in result


def test_provider_failure_returns_safe_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("app.ai.providers.config.OpenAIExtractionProvider", FakeProvider)
    monkeypatch.setattr(FakeProvider, "failure", True)
    path = tmp_path / "claim.txt"
    path.write_text("A vehicle was stolen", encoding="utf-8")
    assert cli.main(["extract", "--text-file", str(path)]) == 1
    assert capsys.readouterr().err.strip() == "Extraction failed: authentication"


def test_missing_configuration_is_reported(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert cli.main(["extract", "--claim-id", str(uuid4())]) == 1
    assert "OPENAI_MODEL and OPENAI_API_KEY are required" in capsys.readouterr().err


def test_gemini_selected_without_openai_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("app.ai.providers.config.GeminiExtractionProvider", FakeProvider)
    path = tmp_path / "claim.txt"
    path.write_text("A vehicle was stolen", encoding="utf-8")
    assert cli.main(["extract", "--text-file", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["extracted_data"]["incident_type"] == "theft"


def test_unknown_provider_is_rejected(monkeypatch, capsys):
    monkeypatch.setenv("AI_PROVIDER", "unknown")
    assert cli.main(["extract", "--claim-id", str(uuid4())]) == 1
    assert "AI_PROVIDER must be openai or gemini" in capsys.readouterr().err
