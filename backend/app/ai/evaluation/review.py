"""Human review of output facts, kept separate from label comparison."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

FACT_FIELDS = ("incident_type", "incident_date", "policy_number", "damaged_parts", "estimated_repair_cost", "currency", "uncertainties")
VERDICTS = {"supported", "unsupported", "unclear"}


def facts_for_review(case_id, actual, label_evidence):
    """Queue every emitted fact, including matching labels and free-text uncertainties."""
    items = []
    for field in FACT_FIELDS:
        value = actual.get(field)
        values = value if isinstance(value, list) else ([] if value is None else [value])
        for index, item in enumerate(values):
            items.append({"id": f"{case_id}/{field}/{index}", "field": field, "value": item,
                          "label_evidence": label_evidence.get(field, []), "verdict": None,
                          "evidence": [], "rationale": "", "reviewer": ""})
    return items


def support_summary(rows):
    counts = Counter(item["verdict"] or "unreviewed" for row in rows for item in row.get("support_review", []))
    return {"total": sum(counts.values()), "reviewed": sum(counts[x] for x in VERDICTS),
            "supported": counts["supported"], "unsupported": counts["unsupported"],
            "unclear": counts["unclear"], "unreviewed": counts["unreviewed"]}


def results_fingerprint(report):
    """Bind reviews to the original claims, outputs, and scoring, excluding judgments."""
    case_fields = ("id", "description", "valid", "actual", "field_matches", "missing_fields_correct",
                   "uncertainty_handled", "actual_missing_fields", "error_code")
    content = {"run_id": report["run_id"], "dataset_sha256": report["dataset_sha256"],
               "label_review": report.get("label_review"), "label_review_verified": report.get("label_review_verified"),
               "cases": [{key: row[key] for key in case_fields if key in row} for row in report["cases"]]}
    data = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def review_template(report):
    import copy
    return {"run_id": report["run_id"], "dataset_sha256": report["dataset_sha256"],
            "results_sha256": results_fingerprint(report), "attestation": {"reviewer": "", "independent_human": False},
            "instructions": "An independent human reviewer must set the attestation. For each emitted fact, read the entire claim. Set verdict to supported, unsupported, unclear, or null. For reviewed facts provide the same reviewer and a rationale; supported facts also require an exact source excerpt. Label evidence is a hint, not a verdict.",
            "cases": [{"id": row["id"], "description": row["description"], "facts": copy.deepcopy(row["support_review"])}
                      for row in report["cases"] if row["valid"]]}


def apply_review(report, review):
    """Apply only explicit, evidence-linked reviews to the original run's facts."""
    if (review.get("run_id") != report.get("run_id") or review.get("dataset_sha256") != report.get("dataset_sha256")
            or review.get("results_sha256") != results_fingerprint(report)):
        raise ValueError("Review does not belong to this evaluation run, dataset, and original results")
    import copy
    result = copy.deepcopy(report)
    for row in result["cases"]:
        if not row["valid"]:
            continue
        expected = facts_for_review(row["id"], row["actual"], {})
        identity = lambda item: (item["id"], item["field"], item["value"])
        if [identity(item) for item in row.get("support_review", [])] != [identity(item) for item in expected]:
            raise ValueError("Review facts do not match the model output")
    attestation = review.get("attestation") or {}
    attester = attestation.get("reviewer")
    originals = {item["id"]: (row, item) for row in result["cases"] for item in row.get("support_review", [])}
    seen = set()
    for case in review.get("cases", []):
        for entry in case.get("facts", []):
            item_id = entry.get("id")
            if item_id in seen or item_id not in originals:
                raise ValueError("Unknown or duplicate review item")
            seen.add(item_id)
            row, original = originals[item_id]
            if case.get("id") != row["id"] or entry.get("field") != original["field"] or entry.get("value") != original["value"]:
                raise ValueError("Review item does not match the evaluated fact")
            verdict = entry.get("verdict")
            if verdict is None:
                continue
            if verdict not in VERDICTS:
                raise ValueError("Verdict must be supported, unsupported, unclear, or null")
            if attestation.get("independent_human") is not True or not isinstance(attester, str) or not attester.strip():
                raise ValueError("Reviewed facts require an independent-human self-attestation")
            reviewer, rationale, evidence = entry.get("reviewer"), entry.get("rationale"), entry.get("evidence")
            if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(rationale, str) or not rationale.strip():
                raise ValueError("Each reviewed fact needs a reviewer and rationale")
            if reviewer.strip() != attester.strip():
                raise ValueError("Each fact reviewer must match the self-attested reviewer")
            if not isinstance(evidence, list) or any(not isinstance(x, str) or not x or x not in row["description"] for x in evidence):
                raise ValueError("Evidence must be a list of exact excerpts from the claim")
            if verdict == "supported" and not evidence:
                raise ValueError("Supported facts need at least one exact source excerpt")
            from datetime import datetime, timezone
            original.update(verdict=verdict, evidence=evidence, rationale=rationale.strip(), reviewer=reviewer.strip(),
                            review_method="independent_human_self_attested", reviewed_at=datetime.now(timezone.utc).isoformat())
    result["support_summary"] = support_summary(result["cases"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare or apply a human support review")
    parser.add_argument("--results", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--template", type=Path)
    group.add_argument("--apply", type=Path)
    parser.add_argument("--output", type=Path, help="New reviewed JSON report; required with --apply")
    parser.add_argument("--report", type=Path, help="New reviewed Markdown report; required with --apply")
    args = parser.parse_args(argv)
    result = json.loads(args.results.read_text(encoding="utf-8"))
    if args.template:
        if args.template.resolve() == args.results.resolve():
            parser.error("Template and results paths must differ")
        args.template.write_text(json.dumps(review_template(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Review template written to {args.template}")
        return 0
    if not args.output or not args.report:
        parser.error("--output and --report are required with --apply")
    paths = [path.resolve() for path in (args.results, args.apply, args.output, args.report)]
    if len(set(paths)) != len(paths):
        parser.error("Results, review, output, and report paths must be different")
    from app.ai.evaluation.runner import markdown
    reviewed = apply_review(result, json.loads(args.apply.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.write_text(markdown(reviewed), encoding="utf-8")
    print(f"Reviewed results written to {args.output} and {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
