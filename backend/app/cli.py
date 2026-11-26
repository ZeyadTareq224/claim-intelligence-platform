"""Command-line entry point for structured claim extraction."""
import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.ai.providers.base import ProviderError
from app.ai.providers.config import make_provider
from app.domain.models import AnalysisStatus
from app.persistence.db import make_sessions
from app.repositories.claims import ClaimNotFound, ClaimRepository
from app.repositories.documents import DocumentRepository
from app.services.claim_service import ClaimExtractionService
from app.services.pdf_input import PdfInputError, PdfInputService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract claim facts without changing the claim")
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract")
    source = extract.add_mutually_exclusive_group(required=True)
    source.add_argument("--claim-id", type=UUID)
    source.add_argument("--text-file", type=Path)
    validate = commands.add_parser("validate-pdf", help="validate and preserve a PDF before ingestion")
    validate.add_argument("--source-type", choices=("policy", "claim"), required=True)
    validate.add_argument("--document-id", type=UUID, required=True)
    validate.add_argument("--file", type=Path, required=True)
    inspect = commands.add_parser("inspect-document", help="inspect document processing runs")
    inspect.add_argument("--source-type", choices=("policy", "claim"), required=True)
    inspect.add_argument("--document-id", type=UUID, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-pdf":
            result = PdfInputService.from_environment(make_sessions()).validate(
                args.source_type, args.document_id, args.file)
            print(json.dumps({"source_type": result.source_type, "document_id": str(result.document_id),
                              "sha256": result.sha256, "page_count": result.page_count}))
            return 0
        if args.command == "inspect-document":
            runs = DocumentRepository(make_sessions()).inspect(args.source_type, args.document_id)
            print(json.dumps({"source_type": args.source_type, "document_id": str(args.document_id),
                              "runs": runs}, ensure_ascii=False, indent=2))
            return 0
        provider = make_provider()
        if args.claim_id is not None:
            repository = ClaimRepository(make_sessions())
            result = ClaimExtractionService(provider, repository).extract_claim(args.claim_id)
            payload = {
                "analysis_id": str(result.id), "status": result.status.value,
                "extracted_data": result.extracted_data, "missing_fields": result.missing_fields,
                "uncertainties": result.uncertainties, "error_code": result.error_code,
                "provider": result.provider, "model_name": result.model_name, "prompt_version": result.prompt_version,
                "input_tokens": result.input_tokens, "output_tokens": result.output_tokens, "latency_ms": result.latency_ms,
                "input_fingerprint": result.input_fingerprint, "had_policy_association": result.had_policy_association,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1 if result.status == AnalysisStatus.FAILED else 0
        text = args.text_file.read_text(encoding="utf-8")
        result = ClaimExtractionService(provider).extract_text(text)
        print(json.dumps({
            "extracted_data": result.extracted_data.model_dump(mode="json"),
            "missing_fields": result.missing_fields, "uncertainties": result.extracted_data.uncertainties,
            "provider": provider.provider_name, "model_name": provider.model_name,
            "prompt_version": provider.prompt_version, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens, "latency_ms": result.latency_ms,
        }, ensure_ascii=False, indent=2))
        return 0
    except PdfInputError as exc:
        print(f"PDF input error: {exc.code}", file=sys.stderr)
    except ProviderError as exc:
        print(f"Extraction failed: {exc.code}", file=sys.stderr)
    except (ValueError, ClaimNotFound) as exc:
        print(f"Input or configuration error: {exc}", file=sys.stderr)
    except (OSError, UnicodeError):
        print("Input error: could not read the text file as UTF-8", file=sys.stderr)
    except SQLAlchemyError:
        print("Database error: could not complete the requested operation", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
