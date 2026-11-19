"""Evaluate extraction correctness and prepare separate human support reviews."""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from app.ai.evaluation.label_review import dataset_fingerprint, is_reviewed
from app.ai.evaluation.review import facts_for_review, support_summary
from app.ai.extraction import ClaimExtraction, missing_fields
from app.ai.providers.base import ProviderError
from app.ai.providers.config import make_provider
from app.services.claim_service import ClaimExtractionService

DATASET = Path(__file__).with_name("dataset-v2.json")
FIELDS = ("incident_type", "incident_date", "policy_number", "damaged_parts", "estimated_repair_cost", "currency")
ALIASES = {
    "bonnet": "hood", "car hood": "hood", "front car bumper": "front bumper",
    "front left headlight": "left headlight", "left front headlight": "left headlight",
    "engine compartment": "engine", "front fender": "front fender",
    "المصد الأمامي": "front bumper", "الصدام الأمامي": "front bumper", "المصد الامامي": "front bumper",
    "الإكصدام الأمامي": "front bumper", "غطاء المحرك": "hood", "المصباح الأمامي الأيسر": "left headlight",
    "الزجاج الأمامي": "windshield", "المحرك": "engine", "الباب الخلفي": "rear door",
}
TOPIC_TERMS = {
    "incident_date": ("date", "day", "2023-", "2024-", "التاريخ", "تاريخ", "يوم"),
    "incident_type": ("incident", "type", "theft", "stolen", "fire", "نوع", "حادث", "سرقة", "حريق"),
    "policy_number": ("policy", "number", "وثيقة", "بوليصة", "رقم"),
    "estimated_repair_cost": ("amount", "cost", "estimate", "repair", "مبلغ", "تكلفة", "تقدير", "إصلاح"),
    "currency": ("currency", "dollar", "euro", "usd", "eur", "عملة", "دولار", "يورو"),
    "damaged_parts": ("damage", "hood", "part", "ضرر", "تلف", "محرك", "غطاء"),
}


def normalize(field, value):
    if value is None:
        return None
    if field == "damaged_parts":
        cleaned = (" ".join(str(item).casefold().split()) for item in value)
        return sorted({ALIASES.get(item, item) for item in cleaned})
    if field == "estimated_repair_cost":
        return Decimal(str(value))
    if field == "currency":
        return str(value).strip().upper()
    if field == "policy_number":
        return str(value).strip()
    return str(value)


def validate_dataset(dataset):
    if not isinstance(dataset.get("version"), str) or not dataset.get("cases"):
        raise ValueError("Evaluation dataset requires a version and at least one case")
    seen = set()
    for case in dataset["cases"]:
        if not case.get("id") or case["id"] in seen:
            raise ValueError("Evaluation case IDs must be unique and nonempty")
        seen.add(case["id"])
        description = case.get("description")
        if not isinstance(description, str) or not description.strip() or len(description) > 20000:
            raise ValueError(f"Case {case['id']} needs a nonempty description of at most 20000 characters")
        expected = ClaimExtraction.model_validate(case["expected"])
        if sorted(missing_fields(expected)) != sorted(case["missing_fields"]):
            raise ValueError(f"Case {case['id']} has inconsistent missing-field labels")
        topic = case.get("uncertainty_topic")
        if topic not in (None, *TOPIC_TERMS):
            raise ValueError(f"Case {case['id']} has an unknown uncertainty topic")
        if topic and getattr(expected, topic) not in (None, []):
            raise ValueError(f"Case {case['id']} must leave its disputed field unknown")
        if "source_evidence" in case:
            evidence = case["source_evidence"]
            if not isinstance(evidence, dict) or set(evidence) - set(FIELDS):
                raise ValueError(f"Case {case['id']} has invalid evidence fields")
            for field, excerpts in evidence.items():
                if not isinstance(excerpts, list) or not excerpts or any(not isinstance(x, str) or not x or x not in description for x in excerpts):
                    raise ValueError(f"Case {case['id']} has evidence not present in the description")
            for field in FIELDS:
                if (getattr(expected, field) not in (None, []) or field == topic) and not evidence.get(field):
                    raise ValueError(f"Case {case['id']} needs evidence for {field}")
        elif dataset.get("evidence_required", False):
            raise ValueError(f"Case {case['id']} requires source evidence")


def score_case(case, extraction, actual_missing):
    actual = extraction.model_dump(mode="json")
    expected = case["expected"]
    field_matches = {field: normalize(field, actual[field]) == normalize(field, expected[field]) for field in FIELDS}
    topic = case.get("uncertainty_topic")
    uncertainty_ok = None
    if topic:
        joined = " ".join(extraction.uncertainties).casefold()
        uncertainty_ok = actual[topic] in (None, []) and any(term in joined for term in TOPIC_TERMS[topic])
    return {"field_matches": field_matches, "missing_fields_correct": sorted(actual_missing) == sorted(case["missing_fields"]),
            "uncertainty_handled": uncertainty_ok, "actual": actual, "actual_missing_fields": actual_missing,
            "support_review": facts_for_review(case["id"], actual, case.get("source_evidence", {}))}


def evaluate(dataset, provider, *, interval_seconds=0):
    """Score labels; callers decide whether the dataset is approved for live use."""
    validate_dataset(dataset)
    if not math.isfinite(interval_seconds) or interval_seconds < 0:
        raise ValueError("Interval must be a finite nonnegative number")
    service = ClaimExtractionService(provider)
    rows = []
    for index, case in enumerate(dataset["cases"]):
        if index and interval_seconds:
            sleep(interval_seconds)
        print(f"Evaluating {index + 1}/{len(dataset['cases'])}: {case['id']}", flush=True)
        start = monotonic()
        try:
            result = service.extract_text(case["description"])
            row = {"id": case["id"], "scenario": case["scenario"], "description": case["description"], "valid": True,
                   **score_case(case, result.extracted_data, result.missing_fields),
                   "input_tokens": result.input_tokens, "output_tokens": result.output_tokens, "latency_ms": result.latency_ms}
        except ProviderError as exc:
            row = {"id": case["id"], "scenario": case["scenario"], "description": case["description"],
                   "valid": False, "error_code": exc.code, "latency_ms": round((monotonic() - start) * 1000),
                   "input_tokens": None, "output_tokens": None}
        rows.append(row)
    valid = [row for row in rows if row["valid"]]
    field_correct = Counter(field for row in valid for field, correct in row["field_matches"].items() if correct)
    ambiguity = [row for row in valid if row["uncertainty_handled"] is not None]
    return {
        "run_id": str(uuid4()), "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_version": dataset["version"], "dataset_sha256": dataset_fingerprint(dataset),
        "label_review": dataset.get("label_review", {"status": "pending"}),
        "label_review_verified": is_reviewed(dataset),
        "provider": provider.provider_name, "model": provider.model_name, "prompt_version": provider.prompt_version,
        "total": len(rows), "schema_valid": len(valid),
        "provider_errors": dict(Counter(row["error_code"] for row in rows if not row["valid"])),
        "fields": {field: {"correct": field_correct[field], "evaluated": len(valid)} for field in FIELDS},
        "missing_correct": sum(row["missing_fields_correct"] for row in valid), "missing_evaluated": len(valid),
        "uncertainty_correct": sum(row["uncertainty_handled"] is True for row in ambiguity),
        "uncertainty_evaluated": len(ambiguity), "support_summary": support_summary(rows),
        "latency_ms": {"mean": round(statistics.mean(row["latency_ms"] for row in rows), 1),
                       "min": min(row["latency_ms"] for row in rows), "max": max(row["latency_ms"] for row in rows)},
        "input_tokens": sum(row["input_tokens"] for row in rows if row["input_tokens"] is not None),
        "input_usage_available": sum(row["input_tokens"] is not None for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows if row["output_tokens"] is not None),
        "output_usage_available": sum(row["output_tokens"] is not None for row in rows), "cases": rows,
    }


def markdown(report):
    total, valid, support = report["total"], report["schema_valid"], report["support_summary"]
    review = report.get("label_review") or {}
    review_text = (f"independent review self-attested by {review['reviewer']} at {review['reviewed_at']}"
                   if report.get("label_review_verified") else "not independently human-reviewed")
    if report.get("label_review_verified") and review.get("recorded_by"):
        review_text += f"; recorded by {review['recorded_by']}"
    lines = ["# Week 1 — Expanded AI extraction evaluation", "", f"- Run: `{report['run_id']}`",
             f"- Provider: {report['provider']}", f"- Model: `{report['model']}`", f"- Prompt version: `{report['prompt_version']}`",
             f"- Dataset: `{report['dataset_version']}` ({total} synthetic examples)", f"- Dataset SHA-256: `{report['dataset_sha256']}`",
             f"- Label review: {review_text}", f"- Run time: {report['run_at_utc']}", "", "## Method", "",
             "Each claim was processed independently without a trusted policy association. Dates and incident types use canonical schema values; "
             "policy references are trimmed; currencies are uppercase; amounts are compared as decimals. Damage parts are unordered sets with explicit aliases. "
             "Missing fields are exact sets. For ambiguous cases, the disputed field must be unknown and an uncertainty must mention a topic keyword. "
             "This is a heuristic, not semantic judgment. Label evidence is a review aid, not a verdict about model output. "
             "A mismatch is an extraction error under the labels, not proof that a fact is unsupported. "
             "Every emitted scalar, damaged part, and free-text uncertainty is queued separately for human support review; null and empty values make no factual assertion. "
             "No calls modify claim records.", "", "## Results", "",
             f"- Valid structured responses: **{valid}/{total}**. Provider errors: `{report['provider_errors']}`.",
             f"- Correct missing-field sets: **{report['missing_correct']}/{report['missing_evaluated']}** valid responses.",
             f"- Ambiguity heuristic passed: **{report['uncertainty_correct']}/{report['uncertainty_evaluated']}** valid ambiguity cases.",
             (f"- Factual support review: **{support['reviewed']}/{support['total']}** emitted facts reviewed. "
              + (f"Among reviewed facts: {support['supported']} supported, {support['unsupported']} unsupported, "
                 f"{support['unclear']} unclear. {support['unreviewed']} remain unreviewed; this is not an overall unsupported-fact rate."
                 if support['reviewed'] else "Unsupported-fact rate is not measured until facts are reviewed.")),
             f"- Latency: mean {report['latency_ms']['mean']} ms; range {report['latency_ms']['min']}–{report['latency_ms']['max']} ms.",
             f"- Reported input tokens: {report['input_tokens']} across {report['input_usage_available']}/{total} cases; "
             f"output tokens: {report['output_tokens']} across {report['output_usage_available']}/{total} cases.", "", "### Field-level correctness", "",
             "| Field | Correct / valid responses |", "|---|---:|"]
    for field, score in report["fields"].items():
        lines.append(f"| {field} | {score['correct']} / {score['evaluated']} |")
    lines.extend(["", "## Cases needing review", ""])
    problematic = []
    for row in report["cases"]:
        if not row["valid"]:
            problematic.append(f"- `{row['id']}` ({row['scenario']}): provider error `{row['error_code']}`.")
            continue
        flags = ["field mismatches: " + ", ".join(field for field, correct in row["field_matches"].items() if not correct)] if not all(row["field_matches"].values()) else []
        if not row["missing_fields_correct"]: flags.append("missing fields differ")
        if row["uncertainty_handled"] is False: flags.append("ambiguity heuristic failed")
        unsupported = [item["field"] for item in row["support_review"] if item["verdict"] == "unsupported"]
        if unsupported: flags.append("reviewed unsupported facts: " + ", ".join(unsupported))
        if flags: problematic.append(f"- `{row['id']}` ({row['scenario']}): {'; '.join(flags)}.")
    lines.extend(problematic or ["- No field mismatches or provider failures under these rules. Factual support still requires review."])
    lines.extend(["", "## Limitations and suggested improvements", "",
                  "This small synthetic dataset is a baseline, not evidence of production quality. Label review is self-attested; exact labels "
                  "and a finite synonym map can mark valid paraphrases as mismatches. Review support independently using the full claim and source excerpts. "
                  "Free-text uncertainty review and the keyword ambiguity check cannot be reduced to schema validity. "
                  "Provider failures count against schema validity but are excluded from field and completeness denominators. "
                  "Unknown usage is excluded from token totals. See the companion JSON for all individual outputs and reviews.", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the synthetic claim extraction evaluation")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--report", type=Path, default=Path("docs/week-01-evaluation-v2.md"))
    parser.add_argument("--results", type=Path, default=Path("docs/week-01-evaluation-v2.json"))
    parser.add_argument("--interval-seconds", type=float, default=0, help="Delay between cases to respect provider rate limits")
    args = parser.parse_args(argv)
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    validate_dataset(dataset)
    if not is_reviewed(dataset):
        parser.error("Dataset labels need an independent human review recorded for this exact dataset before live evaluation")
    if args.report.resolve() == args.results.resolve():
        parser.error("Report and results paths must differ")
    report = evaluate(dataset, make_provider(), interval_seconds=args.interval_seconds)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    args.report.write_text(markdown(report), encoding="utf-8")
    print(f"Evaluation complete: {report['schema_valid']}/{report['total']} valid; report: {args.report}; details: {args.results}")
    return 0 if report["schema_valid"] == report["total"] else 1
