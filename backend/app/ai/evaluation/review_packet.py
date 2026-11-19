"""Render labels and their source excerpts for independent human review."""
import argparse
import json
from pathlib import Path

from app.ai.evaluation.label_review import dataset_fingerprint, is_reviewed
from app.ai.evaluation.runner import DATASET, validate_dataset


def render(dataset):
    validate_dataset(dataset)
    status = "human review self-attested" if is_reviewed(dataset) else "pending; no independent human label review recorded"
    lines = ["# Expanded Week 1 dataset: independent label review", "", f"**Status: {status}.**", "",
             f"Dataset: `{dataset['version']}` — {len(dataset['cases'])} synthetic cases.",
             f"Dataset SHA-256: `{dataset_fingerprint(dataset)}`", "",
             "An independent human reviewer should inspect every description, expected value, missing-field label, disputed fact, and source excerpt without consulting model outputs. "
             "A literal excerpt is a navigation aid, not proof that the label is semantically correct. "
             "For ambiguous facts, verify that the disputed field is null (or an empty list) and the topic is identified. "
             "The expected uncertainty text is an example, not an exact-match target. "
             "Review English, Arabic, and mixed-language meanings; adjust labels or excerpts in the JSON if needed. "
             "The dataset fingerprint changes when any case changes.", "",
             "After completing the review, a human reviewer can record their self-attestation with the command in the README, "
             "or explicitly confirm in chat so the assistant can record that confirmation. Identity and independence are self-attested.", ""]
    for case in dataset["cases"]:
        lines.extend([f"## {case['id']} — {case['scenario']}", "", f"**Language:** {case.get('language', 'not specified')}", "",
                      "**Description**", "", case["description"], "", "**Expected structured fields**", "", "```json",
                      json.dumps(case["expected"], ensure_ascii=False, indent=2), "```", "",
                      f"**Expected missing fields:** {', '.join(case['missing_fields']) if case['missing_fields'] else 'none'}",
                      f"**Disputed topic:** {case['uncertainty_topic'] or 'none'}", "", "**Label evidence excerpts**", ""])
        for field, excerpts in case.get("source_evidence", {}).items():
            lines.append(f"- `{field}`: " + "; ".join("“" + excerpt + "”" for excerpt in excerpts))
        lines.extend(["", "**Reviewer decision:** not recorded per case", ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare a human label review packet")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path, default=Path("docs/week-01-v2-label-review.md"))
    args = parser.parse_args(argv)
    if args.dataset.resolve() == args.output.resolve():
        parser.error("Dataset and review packet paths must differ")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(dataset), encoding="utf-8")
    print(f"Prepared review packet for {len(dataset['cases'])} cases: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
