import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from google.genai import errors, types

from app.ai.providers.base import ProviderError
from app.ai.providers.gemini_provider import GeminiExtractionProvider


@pytest.fixture
def facts():
    return dict(incident_type="collision", incident_date="2024-03-15", policy_number="P1",
                damaged_parts=["bumper"], estimated_repair_cost="100.00", currency="EGP", uncertainties=[])


@pytest.fixture
def client():
    return SimpleNamespace(models=SimpleNamespace(generate_content=Mock()))


def provider(client):
    return GeminiExtractionProvider(model_name="gemini-test", api_key="not-a-key", client=client)


def response(facts, reason=types.FinishReason.STOP):
    return SimpleNamespace(candidates=[SimpleNamespace(finish_reason=reason)], text=json.dumps(facts),
                           usage_metadata=SimpleNamespace(prompt_token_count=30, candidates_token_count=10,
                                                          thoughts_token_count=5))


def test_structured_output_and_usage(client, facts):
    client.models.generate_content.return_value = response(facts)
    result = provider(client).extract("untrusted claim")
    assert result.extraction.policy_number == "P1"
    assert (result.input_tokens, result.output_tokens) == (30, 15)
    args = client.models.generate_content.call_args.kwargs
    assert args["model"] == "gemini-test" and args["contents"] == "untrusted claim"
    assert args["config"].response_mime_type == "application/json"
    assert "incident_type" in args["config"].response_json_schema["properties"]
    assert "untrusted" in args["config"].system_instruction


@pytest.mark.parametrize("payload,reason", [(None, types.FinishReason.STOP), ('{bad', types.FinishReason.STOP),
                                               ('{}', types.FinishReason.MAX_TOKENS)])
def test_rejected_or_malformed_responses_do_not_return_facts(client, payload, reason):
    client.models.generate_content.return_value = SimpleNamespace(candidates=[SimpleNamespace(finish_reason=reason)], text=payload, usage_metadata=None)
    with pytest.raises(ProviderError, match="invalid_output"):
        provider(client).extract("claim")


@pytest.mark.parametrize("status,expected", [(401, "authentication"), (403, "authentication"), (404, "model_unavailable"), (429, "rate_limit"),
                                               (504, "timeout"), (500, "provider_error")])
def test_api_errors_are_sanitized(client, status, expected):
    client.models.generate_content.side_effect = errors.APIError(status, {"error": {"message": "sensitive payload"}})
    with pytest.raises(ProviderError) as error:
        provider(client).extract("claim")
    assert str(error.value) == expected


def test_timeout_is_sanitized(client):
    client.models.generate_content.side_effect = httpx.ReadTimeout("secret")
    with pytest.raises(ProviderError, match="timeout"):
        provider(client).extract("claim")


def test_client_disables_retries_and_uses_timeout(monkeypatch):
    factory = Mock(return_value=object())
    monkeypatch.setattr("app.ai.providers.gemini_provider.genai.Client", factory)
    GeminiExtractionProvider(model_name="gemini-test", api_key="not-a-key", timeout_seconds=2)
    options = factory.call_args.kwargs["http_options"]
    assert options.timeout == 2000 and options.retry_options.attempts == 1


def test_official_sdk_serializes_and_parses_structured_output(facts):
    from google import genai

    captured = {}

    def handle(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(facts)}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 5, "totalTokenCount": 17},
        })

    transport = httpx.MockTransport(handle)
    http_client = httpx.Client(transport=transport)
    sdk = genai.Client(api_key="not-a-key", http_options=types.HttpOptions(httpx_client=http_client,
                         retry_options=types.HttpRetryOptions(attempts=1)))
    try:
        result = GeminiExtractionProvider(model_name="gemini-test", api_key="not-a-key", client=sdk).extract("claim")
    finally:
        sdk.close()
        http_client.close()
    assert result.extraction.policy_number == "P1"
    assert captured["generationConfig"]["responseMimeType"] == "application/json"
    assert "incident_type" in captured["generationConfig"]["responseJsonSchema"]["properties"]
