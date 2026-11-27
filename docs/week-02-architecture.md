# Week 2 architecture: PDF ingestion and evidence

## Flow and boundaries

The existing Python application remains a modular monolith. The CLI `ingest-pdf` command and the local Streamlit **PDF upload** screen call `DocumentIngestionService`; `inspect-document` and the screen read persisted runs through `DocumentRepository`. There is no HTTP upload API, background worker, vector index, RAG, or coverage decision in this slice.

1. `PdfInputService` checks the existing `PolicyDocument` or `ClaimDocument`, reads a regular local file within the configured size bound, validates the PDF bytes and page count, and rejects malformed or encrypted input. It stores an unchanged, content-addressed copy under its SHA-256 in controlled local storage. Invalid input is rejected before a processing run is created.
2. The ingestion service re-reads the stored bytes, checks their hash, and records the source document, policy document version when present, parser version, detected Tesseract version, configuration, and hash. A claim document has no separate business-version field; its document ID and PDF hash distinguish its source content.
3. PyMuPDF inspects each page. Usable native text is kept; an image-only page, unusable text layer, or detected separate image region is sent to Tesseract. Existing text over an image suppresses OCR for that image to reduce duplication. The resulting route is native, OCR, mixed, or blank. These decisions are heuristics, not a guarantee of finding all text in complex pages.
4. Each page and its ordered blocks are saved in a separate PostgreSQL transaction. A block stores raw text separately from deterministic whitespace-normalized text, a method, an optional bounding box and confidence, and `unknown` as its type when layout semantics are not known. A failed page records an error; it does not erase successful pages.

The configured native timeout applies separately to PDF metadata validation and to native work on each page. Native processing runs in an isolated process that is terminated on timeout; OCR has its own timeout and is not charged to the native budget. The default limits are 25 MiB per file, 200 pages, 30 seconds for native processing, 200 DPI, 25 million OCR pixels, and 60 seconds per OCR call. The Docker image installs Tesseract and English language data. Other configured languages require locally installed data; Arabic has not been evaluated.

## Evidence and coordinates

A run references exactly one existing business document through a foreign key and records the source hash and extraction configuration. Page numbers are one-based and block indices are zero-based. Bounding boxes use unrotated page coordinates in PDF points: top-left origin, x increasing right, y increasing down. Page width, height, and rotation accompany the boxes. OCR pixel boxes are mapped back into that coordinate space. When a native worker times out before returning dimensions, width and height are recorded as zero and coordinates are unavailable.

The UI and CLI display previews and locations. Full raw block text is available only on explicit request using the UI checkbox or the CLI `--full-text` option; normalization does not replace it. Original PDFs are in the configured `PDF_STORAGE_DIR` (the Compose `pdf_data` volume by default); extraction verifies the stored bytes against the recorded hash. Keep that storage alongside database backups: a hash alone cannot reconstruct a lost PDF.

## Status, retries, and concurrency

A newly accepted run moves from `pending` to `processing`, then to `completed`, `partial`, or `failed`. A run is completed only when all expected pages have completed or are blank. A failure with usable blocks yields partial; without usable blocks it yields failed. Validation failures do not create runs. A process interruption can leave a run processing, while already committed pages remain inspectable.

Database uniqueness identifies a run by source document and business version, source hash, and extraction configuration hash. A completed repeat returns the same run. A retry processes only failed or missing pages, preserves successful and blank pages, and replaces a failed page transactionally. Changed source bytes, extractor version, or configuration create a separate run and leave prior provenance intact.

A PostgreSQL session advisory lock claims the identity before creating or resuming a run. A competing attempt receives `processing_in_progress`. The claim and writes share a database connection; losing that connection stops writes and releases the claim. A later attempt can resume an interrupted run. This policy requires a direct PostgreSQL connection or session pooling, not a transaction-pooling proxy.

## Observability and limitations

`app.services.document_ingestion` emits structured-purpose log messages through Python logging: source type and document ID, run ID when available, status, elapsed time, page counts, extraction-method counts, and categorized errors. It does not log PDF paths, full extracted text, OCR stderr, or document bytes. The ingestion CLI configures Python logging at `INFO` when no handlers are already configured; library callers can configure their own handlers. Failures use warning or error levels. CLI inspection and the UI intentionally show text previews, and should be used only where that disclosure is appropriate.

The database enforces exactly one source link and existing claim/policy relationships; organization consistency follows those records. This is not authorization. The local UI has no authentication, and the system is intended for controlled local development with synthetic data. OCR accuracy, reading order, rotated/cropped or overlapping layers, image-region detection, tables, handwriting, and Arabic are not guaranteed. The isolated worker is exercised on Linux and WSL; native Windows operation has not been validated. The fixture evaluation is a small synthetic regression check, not an estimate of production accuracy. See `docs/week-02-evaluation.md`.

## Acceptance audit

- Existing claim or policy documents are validated and processed through `ingest-pdf`; the Streamlit screen invokes the same service. File size, page count, native time, OCR time, OCR DPI, and rendered-pixel limits are configurable.
- Digital, scanned, mixed, blank, malformed, encrypted, OCR failure, page timeout, and retry cases are covered by unit, PostgreSQL integration, and real OCR tests. The routing is deliberately heuristic; in particular, an image that overlaps any native block may be skipped even if it contains additional unrecovered text.
- Runs link to one source record and preserve hash, business policy-document version, extractor configuration, timestamps, page numbers, status, ordered blocks, raw and normalized text, methods, and available coordinates. The fixture evaluator checks source identity, page numbers, hashes and box bounds.
- `completed`, `partial`, and `failed` follow the saved page outcomes. Pending and processing runs survive an interruption and can be resumed after the advisory claim is released. Successful pages are retained across retry.
- The fixture set and measured results, including OCR CER/WER and exact field matches, are in `docs/week-02-evaluation.md`. Real OCR tests run in CI with Tesseract and English data; CI fails if OCR or integration tests are skipped.
- The prior GitHub Actions run for commit `fca7f92` passed. The changes adding this evaluation and audit must still be committed and pushed before GitHub Actions can verify that new revision; a local CI-equivalent run is not a hosted CI result.
