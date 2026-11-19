"""Business completeness checks, independent of any language model provider."""
from app.ai.extraction.schema import ClaimExtraction


def missing_fields(extraction: ClaimExtraction, *, has_verified_policy: bool = False) -> list[str]:
    """Return required missing facts; a verified database policy can satisfy the reference."""
    missing = []
    if extraction.incident_type is None:
        missing.append("incident_type")
    if extraction.incident_date is None:
        missing.append("incident_date")
    if extraction.policy_number is None and not has_verified_policy:
        missing.append("policy_number")
    return missing
