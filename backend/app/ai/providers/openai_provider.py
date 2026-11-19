"""OpenAI Responses API adapter using native structured outputs."""
import json
import math

import openai
from pydantic import ValidationError

from app.ai.extraction import ClaimExtraction
from app.ai.providers.base import ProviderError, ProviderResult

from app.ai.extraction.prompt import PROMPT_VERSION, SYSTEM_INSTRUCTIONS


class OpenAIExtractionProvider:
    provider_name = "openai"
    prompt_version = PROMPT_VERSION

    def __init__(self, *, model_name: str, api_key: str, timeout_seconds: float = 30,
                 max_output_tokens: int = 1000, client=None):
        if not model_name.strip() or not api_key.strip():
            raise ValueError("OPENAI_MODEL and OPENAI_API_KEY are required")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_output_tokens <= 0:
            raise ValueError("OpenAI timeout and output token limit must be positive")
        self.model_name = model_name
        self.max_output_tokens = max_output_tokens
        self.client = client if client is not None else openai.OpenAI(
            api_key=api_key, timeout=timeout_seconds, max_retries=0
        )

    def extract(self, text: str) -> ProviderResult:
        try:
            response = self.client.responses.parse(
                model=self.model_name,
                instructions=SYSTEM_INSTRUCTIONS,
                input=text,
                text_format=ClaimExtraction,
                max_output_tokens=self.max_output_tokens,
                store=False,
            )
            if response.status != "completed" or response.output_parsed is None:
                raise ProviderError("invalid_output")
            # Validate again at the adapter boundary even when using a fake client.
            extraction = ClaimExtraction.model_validate(response.output_parsed)
            usage = response.usage
            return ProviderResult(
                extraction=extraction,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
            )
        except ProviderError:
            raise
        except openai.APITimeoutError as exc:
            raise ProviderError("timeout") from exc
        except openai.AuthenticationError as exc:
            raise ProviderError("authentication") from exc
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("invalid_output") from exc
        except openai.APIConnectionError as exc:
            raise ProviderError("connection") from exc
        except openai.RateLimitError as exc:
            raise ProviderError("rate_limit") from exc
        except openai.OpenAIError as exc:
            raise ProviderError("provider_error") from exc
