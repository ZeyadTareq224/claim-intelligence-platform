"""Load stable fictional Egyptian motor insurance scenarios; never updates existing rows."""
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5
from sqlalchemy.dialects.postgresql import insert
from app.persistence.db import make_sessions
from app.domain.models import (Organization, Vehicle, Policy, PolicyClause, Claim, ClaimDocument, PolicyDocument,
    PolicyType, PolicyStatus, ClauseType, ClaimStatus, ProcessingStatus)


def fixture_id(kind: str, n: int):
    return uuid5(NAMESPACE_URL, f"https://fixtures.claim-intelligence.invalid/{kind}/{n}")


def add(session, model, rows):
    session.execute(insert(model).values(rows).on_conflict_do_nothing())


def seed():
    orgs = [{"id": fixture_id("organization", n), "name": name, "code": code, "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc)} for n, (name, code) in enumerate([("Nile Fictional Insurance", "NILE"), ("Delta Fictional Insurance", "DELTA")])]
    vehicles = [{"id": fixture_id("vehicle", n), "organization_id": fixture_id("organization", n // 5), "vin": f"SYNTHETICVIN{n:05}", "plate_number": f"CAI-{1000+n}", "make": "Synthetic Motors", "model": f"Model {n+1}", "manufacture_year": 2015+n} for n in range(10)]
    policies = []
    clauses = []
    for n in range(12):
        org = n // 6
        vehicle = org * 5 + (n % 6) % 5
        policies.append({"id": fixture_id("policy", n), "organization_id": fixture_id("organization", org), "vehicle_id": fixture_id("vehicle", vehicle), "policy_number": f"POL-EG-{n+1:03}", "policy_type": PolicyType.MOTOR_COMPREHENSIVE if n % 2 == 0 else PolicyType.MOTOR_THIRD_PARTY_LIABILITY, "status": PolicyStatus.EXPIRED if n == 1 else PolicyStatus.ACTIVE, "effective_date": date(2024, 1, 1) if n != 1 else date(2022, 1, 1), "expiration_date": date(2025, 1, 1) if n != 1 else date(2023, 1, 1), "sum_insured": Decimal("500000.00"), "currency": "EGP", "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc)})
        if n % 2 == 0:
            terms = [
                ("COLLISION", ClauseType.COVERAGE, "Collision", "Fictional coverage for accidental damage to the insured vehicle in a collision, subject to all terms.", None),
                ("UNLICENSED", ClauseType.EXCLUSION, "Unlicensed driver", "Fictional exclusion for a driver without a valid licence, subject to policy terms.", None),
                ("DEDUCTIBLE", ClauseType.DEDUCTIBLE, "Collision deductible", "A fictional deductible may apply to collision damage.", Decimal("2500.00")),
            ]
        else:
            terms = [
                ("LIABILITY", ClauseType.COVERAGE, "Third-party liability", "Fictional cover for liability to third parties arising from use of the insured vehicle, subject to all terms.", None),
                ("OWN-DAMAGE", ClauseType.EXCLUSION, "Damage to insured vehicle", "Fictional exclusion for damage to the insured vehicle itself.", None),
                ("NOTICE", ClauseType.CONDITION, "Prompt notification", "Notify the insurer promptly after an incident, subject to policy terms.", None),
            ]
        for k, (code, kind, title, content, deductible) in enumerate(terms):
            clauses.append({"id": fixture_id("clause", n*3+k), "policy_id": fixture_id("policy", n), "clause_code": code, "clause_type": kind, "title": title, "content": content, "deductible_amount": deductible})
    descriptions = [
        "On 2024-03-15 my car was hit in Cairo. The front bumper and left headlight were damaged. Policy POL-EG-001. Repair estimate 12000 EGP.",
        "A collision on 2024-03-15 damaged the rear door. Policy POL-EG-002 expired before the incident.",
        "On 2023-12-15 my car hit a barrier, damaging the front bumper. Policy POL-EG-001.",
        "My car was damaged in a collision on 2024-05-01; I do not know the policy number.",
        "The accident was on 2024-06-01, but later I recorded the date as 2024-06-02. Policy POL-EG-001.",
        "My vehicle was damaged. I cannot remember any other details.",
        "On 2024-02-02 the bumper, hood and two headlights were damaged in a collision. Policy POL-EG-003.",
        "My car was stolen on 2024-04-10. Policy POL-EG-004. No damage was described.",
        "في 2024-05-12 اصطدمت سيارتي بعمود وتضرر الصدام الأمامي. رقم الوثيقة POL-EG-005.",
        "سُرقت السيارة في 2024-07-01 ولم أجد رقم الوثيقة.",
    ]
    claims = []
    for n in range(20):
        org = n // 10
        p = org * 6 + (n % 6)
        if n in (0, 2, 4):
            p = 0
        elif n in (6, 7, 8):
            p = n - 4
        claims.append({"id": fixture_id("claim", n), "organization_id": fixture_id("organization", org), "policy_id": None if n in (3, 9) else fixture_id("policy", p), "claim_number": f"CLM-EG-{n+1:03}", "description": descriptions[n] if n < len(descriptions) else f"On 2024-08-{n-9:02} a parked car was hit and the rear bumper was damaged. Policy POL-EG-{p+1:03}.", "incident_date": None, "reported_at": datetime(2024, 9, 1, tzinfo=timezone.utc), "status": ClaimStatus.SUBMITTED})
    claim_docs = [{"id": fixture_id("claim-document", n), "claim_id": fixture_id("claim", n), "document_type": "incident_report", "filename": f"claim-{n+1}.pdf", "content_type": "application/pdf", "storage_key": f"synthetic/claims/{n+1}.pdf", "processing_status": ProcessingStatus.PENDING, "created_at": datetime(2024, 9, 1, tzinfo=timezone.utc)} for n in range(10)]
    policy_docs = [{"id": fixture_id("policy-document", n), "policy_id": fixture_id("policy", n), "filename": f"policy-{n+1}.pdf", "content_type": "application/pdf", "storage_key": f"synthetic/policies/{n+1}.pdf", "document_version": 1, "uploaded_at": datetime(2024, 1, 1, tzinfo=timezone.utc)} for n in range(5)]
    sessions = make_sessions()
    with sessions.begin() as session:
        for model, rows in [(Organization, orgs), (Vehicle, vehicles), (Policy, policies), (PolicyClause, clauses), (Claim, claims), (ClaimDocument, claim_docs), (PolicyDocument, policy_docs)]:
            add(session, model, rows)
    print("Synthetic fixtures loaded (existing records were preserved).")


if __name__ == "__main__":
    seed()
