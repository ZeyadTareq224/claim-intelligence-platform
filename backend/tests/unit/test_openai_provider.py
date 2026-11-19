from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import openai
import pytest

from app.ai.extraction import ClaimExtraction
from app.ai.providers.base import ProviderError
from app.ai.providers.openai_provider import OpenAIExtractionProvider, SYSTEM_INSTRUCTIONS


@pytest.fixture
def facts():
    return dict(incident_type="collision", incident_date="2024-03-15", policy_number="P1", damaged_parts=["bumper"], estimated_repair_cost="100.00", currency="EGP", uncertainties=[])


@pytest.fixture
def client():
    return SimpleNamespace(responses=SimpleNamespace(parse=Mock()))


def provider(client):
    return OpenAIExtractionProvider(model_name="test-model", api_key="test-key", client=client)


def test_structured_output_and_usage(client, facts):
    client.responses.parse.return_value = SimpleNamespace(status="completed", output_parsed=ClaimExtraction(**facts), usage=SimpleNamespace(input_tokens=100, output_tokens=20))
    result = provider(client).extract("untrusted description")
    assert result.extraction.policy_number == "P1"
    assert (result.input_tokens, result.output_tokens) == (100, 20)
    args = client.responses.parse.call_args.kwargs
    assert args["text_format"] is ClaimExtraction
    assert args["store"] is False and args["input"] == "untrusted description"
    assert "untrusted" in SYSTEM_INSTRUCTIONS and args["max_output_tokens"] == 1000


@pytest.mark.parametrize("payload", [None, {"bad": "data"}])
def test_malformed_output_is_sanitized(client, payload):
    client.responses.parse.return_value = SimpleNamespace(status="completed", output_parsed=payload, usage=None)
    with pytest.raises(ProviderError, match="invalid_output"):
        provider(client).extract("claim")


def test_incomplete_response_is_not_success(client, facts):
    client.responses.parse.return_value = SimpleNamespace(status="incomplete", output_parsed=ClaimExtraction(**facts), usage=None)
    with pytest.raises(ProviderError, match="invalid_output"):
        provider(client).extract("claim")


def test_timeout_is_sanitized(client):
    client.responses.parse.side_effect = openai.APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))
    with pytest.raises(ProviderError, match="timeout"):
        provider(client).extract("claim")


def test_authentication_error_is_sanitized(client):
    response = httpx.Response(401, request=httpx.Request("POST", "https://example.invalid"))
    client.responses.parse.side_effect = openai.AuthenticationError("sensitive provider details", response=response, body=None)
    with pytest.raises(ProviderError) as error:
        provider(client).extract("claim")
    assert str(error.value) == "authentication"


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_rejected(client, timeout):
    with pytest.raises(ValueError):
        OpenAIExtractionProvider(model_name="m", api_key="k", timeout_seconds=timeout, client=client)


def test_sdk_sends_native_structured_output_and_parses_response(facts):
    import json

    captured = {}

    def handle(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_test", "object": "response", "created_at": 1700000000,
            "status": "completed", "model": "test-model", "error": None,
            "output": [{"id": "msg_test", "type": "message", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": json.dumps(facts), "annotations": []}]}],
            "usage": {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
        })

    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    sdk = openai.OpenAI(api_key="test-key", http_client=http_client, max_retries=0)
    try:
        result = OpenAIExtractionProvider(model_name="test-model", api_key="test-key", client=sdk).extract("claim")
    finally:
        sdk.close()
    assert result.extraction.policy_number == "P1"
    assert captured["text"]["format"]["type"] == "json_schema"
    assert captured["text"]["format"]["strict"] is True
    assert captured["store"] is False
