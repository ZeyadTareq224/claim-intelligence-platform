"""Provider-neutral extraction interface and sanitized errors."""
from dataclasses import dataclass
from typing import Protocol

from app.ai.extraction import ClaimExtraction


class ProviderError(Exception):
    """A safe error code; never contains provider response bodies or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProviderResult:
    extraction: ClaimExtraction
    input_tokens: int | None = None
    output_tokens: int | None = None


class ExtractionProvider(Protocol):
    provider_name: str
    model_name: str
    prompt_version: str

    def extract(self, text: str) -> ProviderResult: ...
