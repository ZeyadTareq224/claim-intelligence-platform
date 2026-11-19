from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.ai.extraction import ClaimExtraction, IncidentType, missing_fields


@pytest.fixture
def facts():
    return {
        "incident_type": "collision", "incident_date": "2024-03-15", "policy_number": "POL-EG-001",
        "damaged_parts": ["front bumper", "left headlight"], "estimated_repair_cost": "12000.50",
        "currency": "EGP", "uncertainties": [],
    }


def test_valid_contract_and_decimal(facts):
    result = ClaimExtraction.model_validate(facts)
    assert result.incident_type == IncidentType.COLLISION
    assert result.incident_date == date(2024, 3, 15)
    assert result.repair_cost_decimal == Decimal("12000.50")
    assert missing_fields(result) == []


def test_unknown_facts_and_completeness_are_independent(facts):
    facts.update(incident_type=None, incident_date=None, policy_number=None, damaged_parts=[],
                 estimated_repair_cost=None, currency=None, uncertainties=["Two different dates are reported"])
    result = ClaimExtraction.model_validate(facts)
    assert missing_fields(result) == ["incident_type", "incident_date", "policy_number"]
    assert missing_fields(result, has_verified_policy=True) == ["incident_type", "incident_date"]
    assert result.uncertainties == ["Two different dates are reported"]
    assert result.repair_cost_decimal is None


def test_policy_reference_in_text_satisfies_completeness(facts):
    assert missing_fields(ClaimExtraction.model_validate(facts), has_verified_policy=False) == []


@pytest.mark.parametrize("amount", [42, 42.5, -1, "-1", "1e3", "NaN", " 12 ", ""])
def test_cost_requires_unambiguous_nonnegative_decimal_string(facts, amount):
    facts["estimated_repair_cost"] = amount
    with pytest.raises(ValidationError):
        ClaimExtraction.model_validate(facts)


@pytest.mark.parametrize("currency", ["ZZZ", "egp", "EG", "12$"])
def test_currency_must_be_real_iso_4217_code(facts, currency):
    facts["currency"] = currency
    with pytest.raises(ValidationError):
        ClaimExtraction.model_validate(facts)


@pytest.mark.parametrize("incident_date", ["15/03/2024", "2024-02-30", 1710460800, datetime(2024, 3, 15, tzinfo=timezone.utc)])
def test_dates_are_unambiguous_and_valid(facts, incident_date):
    facts["incident_date"] = incident_date
    with pytest.raises(ValidationError):
        ClaimExtraction.model_validate(facts)


def test_contract_rejects_missing_and_extra_fields(facts):
    del facts["incident_date"]
    with pytest.raises(ValidationError):
        ClaimExtraction.model_validate(facts)
    facts["incident_date"] = None
    facts["coverage_approved"] = True
    with pytest.raises(ValidationError):
        ClaimExtraction.model_validate(facts)


def test_schema_requires_null_not_fabricated_empty_values(facts):
    facts["policy_number"] = " "
    with pytest.raises(ValidationError):
        ClaimExtraction.model_validate(facts)
    facts["policy_number"] = None
    facts["damaged_parts"] = [""]
    with pytest.raises(ValidationError):
        ClaimExtraction.model_validate(facts)


def test_model_response_schema_requires_string_for_amount():
    schema = ClaimExtraction.model_json_schema()
    alternatives = schema["properties"]["estimated_repair_cost"]["anyOf"]
    assert {item["type"] for item in alternatives} == {"string", "null"}
