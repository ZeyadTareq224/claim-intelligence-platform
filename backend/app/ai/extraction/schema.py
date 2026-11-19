"""Provider-independent contract for facts extracted from a claim description."""
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import re

import pycountry
from pydantic import BaseModel, ConfigDict, field_validator


class IncidentType(str, Enum):
    COLLISION = "collision"
    THEFT = "theft"
    FIRE = "fire"
    WEATHER_DAMAGE = "weather_damage"
    OTHER = "other"


class ClaimExtraction(BaseModel):
    """Supported facts only; an absent fact is null or an empty list.

    Repair cost is sent as a decimal string so a model never needs to emit a
    binary floating-point value. Use ``repair_cost_decimal`` for calculations.
    A valid schema cannot establish that the source supports a fact.
    """

    model_config = ConfigDict(extra="forbid")

    incident_type: IncidentType | None
    incident_date: date | None
    policy_number: str | None
    damaged_parts: list[str]
    estimated_repair_cost: str | None
    currency: str | None
    uncertainties: list[str]

    @field_validator("incident_date", mode="before")
    @classmethod
    def iso_date_only(cls, value):
        if isinstance(value, datetime):
            raise ValueError("incident_date must be a date, not a timestamp")
        if value is not None and not isinstance(value, date):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError("incident_date must be an ISO date (YYYY-MM-DD) or null")
        return value

    @field_validator("estimated_repair_cost", mode="before")
    @classmethod
    def decimal_string_only(cls, value):
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value)):
            raise ValueError("estimated_repair_cost must be a nonnegative decimal string or null")
        return value

    @field_validator("currency")
    @classmethod
    def iso_currency(cls, value):
        if value is not None and (not re.fullmatch(r"[A-Z]{3}", value) or pycountry.currencies.get(alpha_3=value) is None):
            raise ValueError("currency must be an uppercase ISO 4217 code or null")
        return value

    @field_validator("policy_number")
    @classmethod
    def nonblank_policy_number(cls, value):
        if value is not None and not value.strip():
            raise ValueError("policy_number must not be blank")
        return value

    @field_validator("damaged_parts", "uncertainties")
    @classmethod
    def nonblank_list_entries(cls, value):
        if any(not item.strip() for item in value):
            raise ValueError("list entries must not be blank")
        return value

    @property
    def repair_cost_decimal(self) -> Decimal | None:
        return Decimal(self.estimated_repair_cost) if self.estimated_repair_cost is not None else None
