"""Orchestrate extraction without keeping a database transaction open during AI calls."""
import hashlib
from dataclasses import dataclass
from time import monotonic
from uuid import UUID

from app.ai.extraction import ClaimExtraction, missing_fields
from app.ai.providers.base import ExtractionProvider, ProviderError
from app.domain.models import AnalysisStatus, ClaimAnalysis
from app.repositories.claims import ClaimRepository

MAX_DESCRIPTION_CHARACTERS = 20000


@dataclass(frozen=True)
class ExtractionResult:
    extracted_data: ClaimExtraction
    missing_fields: list[str]
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


def validate_description(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Claim description must not be empty")
    if len(text) > MAX_DESCRIPTION_CHARACTERS:
        raise ValueError(f"Claim description exceeds {MAX_DESCRIPTION_CHARACTERS} characters")


class ClaimExtractionService:
    def __init__(self, provider: ExtractionProvider, repository: ClaimRepository | None = None):
        self.provider = provider
        self.repository = repository

    def extract_text(self, text: str, *, has_verified_policy: bool = False) -> ExtractionResult:
        validate_description(text)
        started = monotonic()
        response = self.provider.extract(text)
        latency = max(0, round((monotonic() - started) * 1000))
        return ExtractionResult(response.extraction, missing_fields(response.extraction, has_verified_policy=has_verified_policy),
                                response.input_tokens, response.output_tokens, latency)

    def extract_claim(self, claim_id: UUID) -> ClaimAnalysis:
        if self.repository is None:
            raise ValueError("A claim repository is required")
        claim_input = self.repository.read_input(claim_id)
        validate_description(claim_input.description)
        started = monotonic()
        result = None
        error_code = None
        try:
            result = self.extract_text(claim_input.description)
        except ProviderError as exc:
            error_code = exc.code
        latency = result.latency_ms if result else max(0, round((monotonic() - started) * 1000))
        analysis = ClaimAnalysis(
            claim_id=claim_id,
            status=AnalysisStatus.COMPLETED if result else AnalysisStatus.FAILED,
            extracted_data=result.extracted_data.model_dump(mode="json") if result else None,
            missing_fields=result.missing_fields if result else [],
            uncertainties=result.extracted_data.uncertainties if result else [],
            provider=self.provider.provider_name,
            model_name=self.provider.model_name,
            prompt_version=self.provider.prompt_version,
            input_tokens=result.input_tokens if result else None,
            output_tokens=result.output_tokens if result else None,
            latency_ms=latency,
            error_code=error_code,
            input_fingerprint=hashlib.sha256(claim_input.description.encode("utf-8")).hexdigest(),
            had_policy_association=claim_input.has_policy_association,
        )
        return self.repository.add_analysis(analysis)
