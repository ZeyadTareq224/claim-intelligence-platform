"""Google Gemini adapter using native JSON-schema structured output."""
import math

import httpx
from google import genai
errors = genai.errors
types = genai.types
from pydantic import ValidationError

from app.ai.extraction import ClaimExtraction
from app.ai.providers.base import ProviderError, ProviderResult
from app.ai.extraction.prompt import PROMPT_VERSION, SYSTEM_INSTRUCTIONS


class GeminiExtractionProvider:
    provider_name = "gemini"
    prompt_version = PROMPT_VERSION

    def __init__(self, *, model_name: str, api_key: str, timeout_seconds: float = 30,
                 max_output_tokens: int = 2048, client=None):
        if not model_name.strip() or not api_key.strip():
            raise ValueError("GEMINI_MODEL and GEMINI_API_KEY are required")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_output_tokens <= 0:
            raise ValueError("Gemini timeout and output token limit must be positive")
        self.model_name = model_name
        self.max_output_tokens = max_output_tokens
        self.client = client if client is not None else genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=max(1, int(timeout_seconds * 1000)),
                                           retry_options=types.HttpRetryOptions(attempts=1)),
        )

    def extract(self, text: str) -> ProviderResult:
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=text,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTIONS,
                    response_mime_type="application/json",
                    response_json_schema=ClaimExtraction.model_json_schema(),
                    max_output_tokens=self.max_output_tokens,
                ),
            )
            # Never treat a truncated or blocked response as a completed analysis.
            if not response.candidates or response.candidates[0].finish_reason != types.FinishReason.STOP:
                raise ProviderError("invalid_output")
            if not response.text:
                raise ProviderError("invalid_output")
            extraction = ClaimExtraction.model_validate_json(response.text)
            usage = response.usage_metadata
            input_tokens = usage.prompt_token_count if usage else None
            output_tokens = usage.candidates_token_count if usage else None
            if output_tokens is not None and usage.thoughts_token_count is not None:
                output_tokens += usage.thoughts_token_count
            return ProviderResult(extraction=extraction, input_tokens=input_tokens, output_tokens=output_tokens)
        except ProviderError:
            raise
        except (ValidationError, ValueError) as exc:
            raise ProviderError("invalid_output") from exc
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderError("timeout") from exc
        except httpx.ConnectError as exc:
            raise ProviderError("connection") from exc
        except errors.APIError as exc:
            code = {401: "authentication", 403: "authentication", 404: "model_unavailable", 408: "timeout", 429: "rate_limit", 504: "timeout"}.get(exc.code, "provider_error")
            raise ProviderError(code) from exc
