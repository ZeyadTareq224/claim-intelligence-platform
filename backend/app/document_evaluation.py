"""Evaluate synthetic PDF fixtures through ingestion and persisted provenance."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import UUID

from sqlalchemy import func, select

from app.domain.models import DocumentProcessingRun, PolicyDocument
from app.persistence.db import make_sessions
from app.services.document_ingestion import DocumentIngestionService
from app.services.pdf_input import PdfInputError


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pdf"


def distance(expected, actual):
    """Levenshtein edit distance for characters or words."""
    previous = list(range(len(actual) + 1))
    for i, left in enumerate(expected, 1):
        current = [i]
        for j, right in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def normalized(text):
    return " ".join(text.upper().split())


def fields(text):
    policy = re.search(r"\bPOLICY NUMBER\s+([A-Z0-9-]+)", text)
    amount = re.search(r"\bSUM INSURED\s+(EGP\s+[0-9]+)", text)
    return {"policy_number": policy.group(1) if policy else None,
            "amount": amount.group(1) if amount else None}


def evaluate(sessions, document_id, fixtures=FIXTURES):
    manifest = json.loads((fixtures / "ground-truth.json").read_text(encoding="utf-8"))
    service = DocumentIngestionService.from_environment(sessions)
    with sessions() as session:
        document = session.get(PolicyDocument, document_id)
        if document is None:
            raise ValueError("evaluation policy document not found")
        document_version = document.document_version
    totals = {"pages": 0, "nonblank_pages": 0, "native_expected": 0, "native_covered": 0,
              "ocr_fallback_pages": 0, "method_correct": 0, "provenance_correct": 0,
              "policy_correct": 0, "policy_total": 0, "amount_correct": 0, "amount_total": 0,
              "ocr_char_edits": 0, "ocr_reference_chars": 0, "ocr_word_edits": 0, "ocr_reference_words": 0,
              "hybrid_char_edits": 0, "hybrid_reference_chars": 0, "hybrid_word_edits": 0, "hybrid_reference_words": 0}
    results = []
    for case in manifest["cases"]:
        path = fixtures / case["file"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != case["sha256"]:
            raise ValueError(f"fixture hash mismatch: {case['file']}")
        if "expected_error" in case:
            with sessions() as session:
                before = session.scalar(select(func.count()).select_from(DocumentProcessingRun).where(DocumentProcessingRun.policy_document_id == document_id))
            try:
                service.ingest("policy", document_id, path)
                error = None
            except PdfInputError as exc:
                error = exc.code
            with sessions() as session:
                after = session.scalar(select(func.count()).select_from(DocumentProcessingRun).where(DocumentProcessingRun.policy_document_id == document_id))
            results.append({"file": case["file"], "expected_error": case["expected_error"],
                            "actual_error": error, "no_new_run": before == after})
            continue
        run_id = service.ingest("policy", document_id, path)
        with sessions() as session:
            run = session.get(DocumentProcessingRun, run_id)
            by_number = {page.page_number: page for page in run.pages}
            run_provenance = (run.policy_document_id == document_id and run.claim_document_id is None
                              and run.policy_document_version == document_version and run.source_hash == digest
                              and run.page_count == len(case["pages"]) and len(by_number) == len(case["pages"]))
            pages = []
            for expected in case["pages"]:
                page = by_number.get(expected["number"])
                actual_text = normalized(" ".join(block.raw_text for block in page.blocks)) if page else ""
                reference = normalized(expected["text"])
                method = "blank" if page and page.status == "blank" else page.method if page else None
                status_ok = page is not None and page.status == ("blank" if expected["method"] == "blank" else "completed")
                bounds_ok = page is not None and all(
                    block.bbox is not None and len(block.bbox) == 4
                    and 0 <= block.bbox[0] <= block.bbox[2] <= page.width + 1
                    and 0 <= block.bbox[1] <= block.bbox[3] <= page.height + 1
                    for block in page.blocks)
                provenance = bool(run_provenance and page and page.page_number == expected["number"] and bounds_ok
                                  and (expected["method"] == "blank" or bool(page.blocks)))
                method_ok = status_ok and method == expected["method"]
                totals["pages"] += 1
                totals["provenance_correct"] += int(provenance)
                totals["method_correct"] += int(method_ok)
                if expected["method"] != "blank":
                    totals["nonblank_pages"] += 1
                    totals["ocr_fallback_pages"] += int(method in ("ocr", "mixed"))
                if expected["method"] == "native":
                    totals["native_expected"] += 1
                    totals["native_covered"] += int(method in ("native", "mixed") and status_ok)
                if expected["method"] == "ocr":
                    totals["ocr_char_edits"] += distance(reference, actual_text)
                    totals["ocr_reference_chars"] += len(reference)
                    totals["ocr_word_edits"] += distance(reference.split(), actual_text.split())
                    totals["ocr_reference_words"] += len(reference.split())
                if expected["method"] == "mixed":
                    totals["hybrid_char_edits"] += distance(reference, actual_text)
                    totals["hybrid_reference_chars"] += len(reference)
                    totals["hybrid_word_edits"] += distance(reference.split(), actual_text.split())
                    totals["hybrid_reference_words"] += len(reference.split())
                extracted = fields(actual_text)
                for name, metric in (("policy_number", "policy"), ("amount", "amount")):
                    if expected[name] is not None:
                        totals[f"{metric}_total"] += 1
                        totals[f"{metric}_correct"] += int(extracted[name] == expected[name])
                pages.append({"number": expected["number"], "expected_method": expected["method"],
                              "method": method, "status": page.status if page else "missing",
                              "error": page.error if page else "missing_page", "method_correct": method_ok,
                              "provenance_correct": provenance, "expected_text": reference,
                              "actual_text": actual_text, "expected_policy_number": expected["policy_number"],
                              "actual_policy_number": extracted["policy_number"],
                              "expected_amount": expected["amount"], "actual_amount": extracted["amount"]})
            results.append({"file": case["file"], "source_hash": digest, "run_id": str(run_id),
                            "run_status": run.status, "extractor_config": run.config, "pages": pages})
    totals["ocr_cer"] = totals["ocr_char_edits"] / totals["ocr_reference_chars"] if totals["ocr_reference_chars"] else None
    totals["ocr_wer"] = totals["ocr_word_edits"] / totals["ocr_reference_words"] if totals["ocr_reference_words"] else None
    totals["hybrid_cer"] = totals["hybrid_char_edits"] / totals["hybrid_reference_chars"] if totals["hybrid_reference_chars"] else None
    totals["hybrid_wer"] = totals["hybrid_word_edits"] / totals["hybrid_reference_words"] if totals["hybrid_reference_words"] else None
    passed = (totals["pages"] > 0 and totals["method_correct"] == totals["pages"]
              and totals["provenance_correct"] == totals["pages"]
              and totals["policy_correct"] == totals["policy_total"] > 0
              and totals["amount_correct"] == totals["amount_total"] > 0
              and totals["ocr_cer"] is not None and totals["ocr_cer"] <= 0.2
              and totals["hybrid_cer"] is not None and totals["hybrid_cer"] <= 0.2
              and all(item.get("actual_error") == item.get("expected_error") and item.get("no_new_run")
                      for item in results if "expected_error" in item))
    return {"source_type": "policy", "document_id": str(document_id), "totals": totals,
            "passed": passed, "cases": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate synthetic PDFs against persisted evidence")
    parser.add_argument("--document-id", required=True, type=UUID, help="existing synthetic policy document UUID")
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    args = parser.parse_args(argv)
    try:
        report = evaluate(make_sessions(), args.document_id)
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if report["passed"] else 1
    except (OSError, ValueError) as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
