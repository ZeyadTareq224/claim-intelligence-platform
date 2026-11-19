"""Record an independent human label review for an exact dataset version."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def dataset_fingerprint(dataset):
    content = {key: value for key, value in dataset.items() if key != "label_review"}
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_reviewed(dataset):
    review = dataset.get("label_review") or {}
    if not (review.get("status") == "reviewed" and isinstance(review.get("reviewer"), str)
            and review["reviewer"].strip() and review.get("dataset_sha256") == dataset_fingerprint(dataset)):
        return False
    try:
        timestamp = datetime.fromisoformat(review.get("reviewed_at", ""))
        return timestamp.tzinfo is not None and timestamp.utcoffset() is not None
    except (ValueError, TypeError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record your independent review of all dataset labels")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--reviewer", required=True, help="Human reviewer name or identifier")
    parser.add_argument("--confirm-independent-human-review", action="store_true", required=True,
                        help="Assert that you independently reviewed all descriptions, labels and evidence")
    args = parser.parse_args(argv)
    if not args.reviewer.strip():
        parser.error("reviewer must not be blank")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    from app.ai.evaluation.runner import validate_dataset
    validate_dataset(dataset)
    dataset["label_review"] = {"status": "reviewed", "reviewer": args.reviewer.strip(),
                               "reviewed_at": datetime.now(timezone.utc).isoformat(),
                               "dataset_sha256": dataset_fingerprint(dataset)}
    args.dataset.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded review of {len(dataset['cases'])} labels in {args.dataset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
