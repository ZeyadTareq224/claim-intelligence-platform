"""Run a labeled synthetic dataset and keep separate, auditable metrics."""
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

from app.ai.extraction import ClaimExtraction, missing_fields
from app.ai.providers.base import ProviderError
from app.ai.providers.config import make_provider
from app.services.claim_service import ClaimExtractionService

DATASET = Path(__file__).with_name("dataset.json")
FIELDS = ("incident_type", "incident_date", "policy_number", "damaged_parts", "estimated_repair_cost", "currency")
ALIASES = {
    "bonnet": "hood", "car hood": "hood", "front car bumper": "front bumper",
    "front left headlight": "left headlight", "left front headlight": "left headlight",
    "engine compartment": "engine", "front fender": "front fender",
    "المصد الأمامي": "front bumper", "الصدام الأمامي": "front bumper", "المصد الامامي": "front bumper",
    "غطاء المحرك": "hood", "المصباح الأمامي الأيسر": "left headlight",
}
TOPIC_TERMS = {
    "incident_date": ("date", "day", "september", "august", "2024-", "التاريخ", "تاريخ", "يوم"),
    "incident_type": ("incident", "type", "theft", "stolen", "fire", "نوع", "حادث", "سرقة", "حريق"),
}


def normalize(field: str, value):
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
        if not isinstance(case.get("description"), str) or not case["description"].strip():
            raise ValueError(f"Case {case['id']} needs a description")
        expected = ClaimExtraction.model_validate(case["expected"])
        if sorted(missing_fields(expected)) != sorted(case["missing_fields"]):
            raise ValueError(f"Case {case['id']} has inconsistent missing-field labels")
        topic = case.get("uncertainty_topic")
        if topic not in (None, *TOPIC_TERMS):
            raise ValueError(f"Case {case['id']} has an unknown uncertainty topic")
        if topic and getattr(expected, topic) is not None:
            raise ValueError(f"Case {case['id']} must label its disputed field as null")


def score_case(case, extraction, actual_missing):
    actual = extraction.model_dump(mode="json")
    expected = case["expected"]
    field_matches = {}
    unsupported = []
    for field in FIELDS:
        actual_value = normalize(field, actual[field])
        expected_value = normalize(field, expected[field])
        field_matches[field] = actual_value == expected_value
        if field == "damaged_parts":
            extra = sorted(set(actual_value) - set(expected_value))
            if extra:
                unsupported.append({"field": field, "values": extra})
        elif actual_value is not None and actual_value != expected_value:
            unsupported.append({"field": field, "value": actual[field]})
    topic = case.get("uncertainty_topic")
    uncertainty_ok = None
    if topic:
        joined = " ".join(extraction.uncertainties).casefold()
        uncertainty_ok = actual[topic] is None and any(term in joined for term in TOPIC_TERMS[topic])
    return {"field_matches": field_matches, "missing_fields_correct": sorted(actual_missing) == sorted(case["missing_fields"]),
            "uncertainty_handled": uncertainty_ok, "unsupported_candidates": unsupported, "actual": actual,
            "actual_missing_fields": actual_missing}


def evaluate(dataset, provider, *, interval_seconds=0):
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
            row = {"id": case["id"], "scenario": case["scenario"], "valid": True,
                   **score_case(case, result.extracted_data, result.missing_fields),
                   "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                   "latency_ms": result.latency_ms}
        except ProviderError as exc:
            row = {"id": case["id"], "scenario": case["scenario"], "valid": False,
                   "error_code": exc.code, "latency_ms": round((monotonic() - start) * 1000),
                   "input_tokens": None, "output_tokens": None}
        rows.append(row)
    valid = [row for row in rows if row["valid"]]
    field_correct = Counter(field for row in valid for field, correct in row["field_matches"].items() if correct)
    ambiguity = [row for row in valid if row["uncertainty_handled"] is not None]
    return {
        "run_at_utc": datetime.now(timezone.utc).isoformat(), "dataset_version": dataset["version"],
        "provider": provider.provider_name, "model": provider.model_name, "prompt_version": provider.prompt_version,
        "total": len(rows), "schema_valid": len(valid), "provider_errors": dict(Counter(row["error_code"] for row in rows if not row["valid"])),
        "fields": {field: {"correct": field_correct[field], "evaluated": len(valid)} for field in FIELDS},
        "missing_correct": sum(row["missing_fields_correct"] for row in valid), "missing_evaluated": len(valid),
        "uncertainty_correct": sum(row["uncertainty_handled"] is True for row in ambiguity),
        "uncertainty_evaluated": len(ambiguity),
        "unsupported_candidate_cases": sum(bool(row["unsupported_candidates"]) for row in valid),
        "latency_ms": {"mean": round(statistics.mean(row["latency_ms"] for row in rows), 1),
                       "min": min(row["latency_ms"] for row in rows), "max": max(row["latency_ms"] for row in rows)},
        "input_tokens": sum(row["input_tokens"] for row in rows if row["input_tokens"] is not None),
        "input_usage_available": sum(row["input_tokens"] is not None for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows if row["output_tokens"] is not None),
        "output_usage_available": sum(row["output_tokens"] is not None for row in rows), "cases": rows,
    }


def markdown(report):
    total, valid = report["total"], report["schema_valid"]
    lines = ["# Week 1 — AI extraction evaluation", "", f"- Provider: {report['provider']}",
             f"- Model: `{report['model']}`", f"- Prompt version: `{report['prompt_version']}`",
             f"- Dataset: `{report['dataset_version']}` ({total} synthetic examples)",
             f"- Run time: {report['run_at_utc']}", "", "## Method", "",
             "Labels were authored by a separate project assistant before the evaluated model was called; they have not yet been independently reviewed by a human. Each claim was processed independently without a trusted policy association. "
             "Dates and incident types use their canonical schema values; policy references are trimmed; currencies are uppercase; amounts are compared as decimals. "
             "Damage parts are compared as unordered normalized sets using the explicit aliases in the runner. "
             "Missing fields are compared as exact sets. For ambiguity cases, the disputed field must be null and an uncertainty must contain a topic keyword. "
             "The expected uncertainty arrays are schema placeholders; `uncertainty_topic` defines the semantic expectation for ambiguous cases. This keyword test is a heuristic, not a semantic assessment. No calls modify claim records.", "", "## Results", "",
             f"- Valid structured responses: **{valid}/{total}**. Provider errors: `{report['provider_errors']}`.",
             f"- Correct missing-field sets: **{report['missing_correct']}/{report['missing_evaluated']}** valid responses.",
             f"- Ambiguity/contradiction handling: **{report['uncertainty_correct']}/{report['uncertainty_evaluated']}** valid ambiguity cases.",
             f"- Cases with potentially unsupported facts in the six scored fields: **{report['unsupported_candidate_cases']}/{valid}** valid responses; candidates need human review.",
             f"- Latency: mean {report['latency_ms']['mean']} ms; range {report['latency_ms']['min']}–{report['latency_ms']['max']} ms, including failures.",
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
        bad = [field for field, correct in row["field_matches"].items() if not correct]
        flags = (["fields: " + ", ".join(bad)] if bad else [])
        if not row["missing_fields_correct"]:
            flags.append("missing fields differ")
        if row["uncertainty_handled"] is False:
            flags.append("ambiguity heuristic failed")
        if row["unsupported_candidates"]:
            flags.append("possible unsupported facts: " + ", ".join(x["field"] for x in row["unsupported_candidates"]))
        if flags:
            problematic.append(f"- `{row['id']}` ({row['scenario']}): {'; '.join(flags)}.")
    lines.extend(problematic or ["- None under the scoring rules above."])
    lines.extend(["", "## Limitations and suggested improvements", "",
                  "This small synthetic dataset is a baseline, not evidence of production quality. Exact labels and the limited damage-part alias list can count valid synonyms as errors; "
                  "review candidate unsupported facts and uncertainty wording manually. Free-text uncertainties are checked for relevant keywords on ambiguous cases but are not automatically screened for invented facts. Schema-valid output can still be factually wrong. "
                  "Expand Arabic and English cases and independently review labels, synonyms, and ambiguity scoring before using these metrics to compare models. "
                  "Provider failures are counted against schema validity but excluded from field and completeness denominators. "
                  "Unknown token counts are excluded from totals, and latency includes network time. "
                  "See the companion JSON report for individual results and errors.", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the synthetic claim extraction evaluation")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--report", type=Path, default=Path("docs/week-01-evaluation.md"))
    parser.add_argument("--results", type=Path, default=Path("docs/week-01-evaluation.json"))
    parser.add_argument("--interval-seconds", type=float, default=0, help="Delay between cases to respect provider rate limits")
    args = parser.parse_args(argv)
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    validate_dataset(dataset)  # Fail before making any paid or rate-limited calls.
    report = evaluate(dataset, make_provider(), interval_seconds=args.interval_seconds)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    args.report.write_text(markdown(report), encoding="utf-8")
    print(f"Evaluation complete: {report['schema_valid']}/{report['total']} valid; report: {args.report}; details: {args.results}")
    return 0 if report["schema_valid"] == report["total"] else 1
