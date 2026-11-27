# Claim Intelligence Platform — insurance domain foundation

A PostgreSQL-backed motor insurance domain with synthetic Egyptian fixtures in English and Arabic. This phase covers the database foundation. The AI extraction module supports OpenAI and Gemini structured claim extraction in the same application service.

The backend code, migrations, fixtures, and tests are under `backend/`. See [the domain model](docs/domain-model.md) for the entity relationships and [the extraction contract](docs/ai-extraction.md) for the initial AI schema.

## Requirements

Python 3.12+, PostgreSQL 16+ and Docker Compose. All example credentials below are for local development only.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e './backend[test]' -r frontend/requirements.txt
docker compose up -d --wait db
export DATABASE_URL='postgresql+psycopg://claim:claim_local@localhost:5432/claims'
(cd backend && alembic upgrade head && python -m fixtures.seed)
```

The seeder uses stable IDs and preserves existing rows on repeated runs. It produces entirely synthetic policies and claims; policy wording is fictional and is not a statement of Egyptian law or actual insurance coverage. Application startup does not create tables.

## Tests

Use a separate, disposable PostgreSQL database. Integration tests must not use the fixture database. Create a database called `claims_test` on the local Compose server, migrate it, and run the tests:

```bash
docker compose exec db createdb -U claim claims_test
export TEST_DATABASE_URL='postgresql+psycopg://claim:claim_local@localhost:5432/claims_test'
(cd backend && DATABASE_URL="$TEST_DATABASE_URL" alembic upgrade head)
pytest -q backend/tests frontend/tests
```

If `TEST_DATABASE_URL` is unset, the PostgreSQL integration tests are skipped. Backend tests use transactions and roll back their changes; the UI test creates and deletes a synthetic record in the test database. The test fixture checks that `TEST_DATABASE_URL` differs from `DATABASE_URL`, but different connection strings can still identify the same database; use a genuinely separate database.

GitHub Actions runs Bandit, the unit tests, and PostgreSQL-backed backend and UI integration tests on pushes and pull requests. It creates a separate test database, applies migrations to both databases, and fails if any integration test is skipped.

## Reset local data

`docker compose down -v` permanently deletes the Compose database and PDF storage volumes, including local data, analyses, and saved PDFs. To recreate an empty local database and its fixtures, run that command only when the data is disposable, then repeat the setup steps above.

## Current limitations

Currency constraints enforce uppercase three-letter format, not membership in the ISO 4217 list. Stored policy status alone does not determine coverage. The database enforces tenant-consistent references, but user authentication, authorization, and access-controlled retrieval are not yet implemented. Live model output is not a coverage decision and has not yet been evaluated for factual accuracy. The small synthetic evaluation is an initial baseline and is not evidence of production reliability.

## Run the local data UI

The Compose `platform` service runs the Streamlit UI and the Python backend in one application container. PostgreSQL runs separately. From the project root, start the database, build the application, apply migrations, and load fixtures:

```bash
docker compose up -d --wait db
docker compose build platform
docker compose run --rm platform alembic upgrade head
docker compose run --rm platform python -m fixtures.seed
docker compose up -d platform
```

The platform container connects to PostgreSQL at `db:5432` on the Compose network. If host port 5432 is occupied, export `POSTGRES_PORT=55433` before running the Compose commands. The host connection URL must then use port 55433; the container connection remains on port 5432. Keep the same `POSTGRES_PORT` value for subsequent Compose commands. Open http://localhost:8501 to view and manage the eight entity types. The UI has no authentication and is intended for local use with synthetic data; the port binds to localhost. Select **PDF upload** in the sidebar, choose an existing claim or policy document, and upload a PDF to extract and save its pages. The original PDF is retained in the Compose `pdf_data` volume. Run history shows status, errors, text previews and block coordinates. The database enforces constraints, and deleting a referenced record may fail. Administration commands run in short-lived containers; the application does not apply migrations automatically. The AI module and any later API run in this same application service.

## Extract claim facts with Gemini or OpenAI

For Gemini, set `AI_PROVIDER=gemini`, `GEMINI_API_KEY`, and `GEMINI_MODEL` in the project-root `.env` file. The example model `gemini-3.5-flash-lite` is listed on Google's free tier, subject to project eligibility and quota. Optional `GEMINI_TIMEOUT_SECONDS` and `GEMINI_MAX_OUTPUT_TOKENS` control the request. Check your project's billing tier before calling: a paid project can incur charges. Google states that free-tier content may be used to improve its products, so use only synthetic claims while developing. See [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing).

For OpenAI, set `AI_PROVIDER=openai`, `OPENAI_API_KEY`, and `OPENAI_MODEL`; optional `OPENAI_TIMEOUT_SECONDS` and `OPENAI_MAX_OUTPUT_TOKENS` control requests. OpenAI API requests are billed separately from ChatGPT subscriptions. Docker Compose reads the root `.env` automatically. To use the local Python commands, export the relevant variables in your shell; Python does not automatically load `.env`.

For local Python, set `DATABASE_URL` as above and run from the backend directory:

```bash
cd backend
python -m app.cli extract --claim-id <claim-uuid>
python -m app.cli extract --text-file ../examples/claim.txt
```

For Docker Compose, configure the selected provider in the root `.env` file before running:

```bash
docker compose run --rm platform python -m app.cli extract --claim-id <claim-uuid>
docker compose run --rm platform python -m app.cli extract --text-file examples/claim.txt
```

The claim command creates a new analysis on success or provider failure; it never edits the claim. The file command does not use the database. Failures store a safe error code and no extracted facts. Because the schema does not track whether a policy link is verified, the completeness check currently requires a policy number in the description even if a policy is linked. The analysis records whether a link existed and a SHA-256 fingerprint of the text used, but a fingerprint cannot reconstruct the original text. Provider calls occur outside database transactions. If saving fails after a model call, the command reports a database error and no analysis is guaranteed to have been saved.

## Run the synthetic AI evaluation

The original 12-case baseline remains in `backend/app/ai/evaluation/dataset.json` and `docs/week-01-evaluation.md`. Its old scorer called label mismatches “unsupported candidates”; those counts are not verified hallucination counts.

The expanded 40-case dataset is `backend/app/ai/evaluation/dataset-v2.json`. The user self-attested in chat that all 40 labels were independently reviewed; the assistant recorded that attestation. The live runner refuses to call a provider unless a human review is recorded for the exact dataset content. To review revised labels, first inspect `docs/week-01-v2-label-review.md` against the descriptions, labels, and source excerpts without using model outputs. A source excerpt is a review aid, not automatic proof. Edit the dataset if corrections are needed, then regenerate the packet:

```bash
python -m app.ai.evaluation.review_packet
```

After personally and independently reviewing all cases, a human reviewer can self-attest using a name or identifier:

```bash
python -m app.ai.evaluation.label_review \
  --dataset backend/app/ai/evaluation/dataset-v2.json \
  --reviewer "reviewer identifier" \
  --confirm-independent-human-review
```

The attestation is not proof of identity; it records the reviewer's assertion and a dataset fingerprint. Any subsequent label or evidence change invalidates it. A reviewer may run this command directly. A recorded chat attestation must identify that it was recorded by an assistant on the reviewer's explicit confirmation. The local Python environment must have the project dependencies installed as described in Setup.

After review, rebuild the image so it includes the reviewed dataset, then run from the project root:

```bash
docker compose build platform
docker compose run --rm -v "$PWD/docs:/opt/claim-platform/docs" platform \
  python -m app.ai.evaluation --interval-seconds 6
```

The run writes `docs/week-01-evaluation-v2.md` and `.json`, leaving the original baseline untouched. Repeated runs overwrite the v2 report files. Live runs consume provider quota and may incur charges on paid projects; the delay does not guarantee quota availability. Use only synthetic data on Gemini's free tier.

### Review factual support

A field mismatch is reported as an extraction error, not automatically as an unsupported fact. The runner queues every emitted fact and uncertainty as **unreviewed**, including facts matching a label. A human must judge support against the full source claim. Until facts are reviewed, the unsupported-fact rate is not measured.

Create a review template tied to a specific run and its exact results:

```bash
python -m app.ai.evaluation.review \
  --results docs/week-01-evaluation-v2.json \
  --template docs/week-01-support-review-v2.json
```

For each fact, use `supported` when the claim establishes it, `unsupported` when the claim does not substantiate it, and `unclear` when the source or the judgment is ambiguous. For example, if a claim gives two possible dates and the model selects one, mark the field mismatch separately; the date's presence alone does not establish it as the incident date or prove it was invented. Use `unclear` when the support judgment cannot be resolved. Excerpts must be read in context, including negations and instructions embedded in the claim.

An independent human reviewer fills the template's `attestation` (`reviewer` identifier and `independent_human: true`) and each reviewed fact's matching `reviewer`, `verdict` (`supported`, `unsupported`, or `unclear`), and `rationale`. A `supported` verdict also needs at least one exact excerpt from the claim in `evidence`; excerpts are review aids and do not themselves prove support. Leave `verdict` as null for anything not reviewed. Apply the review to new files:

```bash
python -m app.ai.evaluation.review \
  --results docs/week-01-evaluation-v2.json \
  --apply docs/week-01-support-review-v2.json \
  --output docs/week-01-evaluation-v2-reviewed.json \
  --report docs/week-01-evaluation-v2-reviewed.md
```

Reviewed support counts apply only to the facts actually reviewed; unreviewed and unclear facts remain separate. The review tool validates the run, dataset, model output, and source excerpts, but the human's semantic judgment and identity are self-attested.

## Validate a PDF for ingestion

The `validate-pdf` command checks and preserves a PDF without extracting text or creating a processing run. From `backend/`, with `DATABASE_URL` set:

```bash
python -m app.cli validate-pdf --source-type policy --document-id <document-uuid> --file /path/to/document.pdf
```

Use `--source-type claim` for a claim document. The command checks the record, reads the file as a PDF (regardless of extension), rejects encrypted, malformed, empty and over-limit PDFs, and prints the SHA-256 and page count. It stores the unchanged bytes under their hash in `.document-storage/` relative to the working directory; set `PDF_STORAGE_DIR` to an application-controlled persistent directory to change that location. Defaults are 25 MiB (`PDF_MAX_BYTES`) and 200 pages (`PDF_MAX_PAGES`). Do not use temporary storage if the preserved source must remain available. This local operator command does not provide authentication or authorization.

## PDF page extraction service

`app.services.pdf_extraction.PdfExtractor` returns page results with ordered native or OCR text blocks, method, bounding boxes and page-level errors. The native processing limit is `PDF_NATIVE_TIMEOUT_SECONDS` (default 30 seconds per page and per document metadata inspection). CLI and frontend OCR settings are `PDF_OCR_LANGUAGE` (default `eng`), `PDF_OCR_DPI` (200), `PDF_OCR_TIMEOUT_SECONDS` (60), and `PDF_OCR_MAX_PIXELS` (25000000). The Docker image installs Tesseract and English data; local installations need the `tesseract` executable and the requested language data. Arabic is configurable if its language data is installed, but has not been evaluated.

Text usability and image-region detection are heuristics; complex layouts, image-only blank pages, overlapping image and text layers, and reading order can be misclassified. A failed OCR region retains any native blocks but marks the page failed. Bounding boxes use unrotated page coordinates in PDF points. Native page processing runs in a separate process and records `native_timeout` if it exceeds the limit; OCR has its own timeout. OCR time is not counted against the native limit. If a worker times out before returning dimensions, the page dimensions are stored as zero and its coordinates are unavailable.

Run the real OCR smoke tests with Tesseract and English language data installed and `TEST_DATABASE_URL` set to a separate migrated PostgreSQL database:

```bash
cd backend
pytest -q tests/ocr
```

## Store extracted document evidence

Apply the latest Alembic migration, then `DocumentIngestionService` can validate a PDF, extract the preserved bytes, and store a processing run with its document reference, source hash, configuration, pages, and ordered blocks in PostgreSQL. Policy document versions are recorded at processing time. Raw block text remains separate from normalized text. From `backend/`, with `DATABASE_URL` set, process a PDF and inspect its runs:

```bash
python -m app.cli ingest-pdf --source-type policy --document-id <document-uuid> --file /path/to/document.pdf
python -m app.cli inspect-document --source-type policy --document-id <document-uuid>
```

Use `--source-type claim` for a claim document. Ingestion returns a nonzero exit code for partial or failed runs. Output includes short text previews and coordinates. Add `--full-text` to `inspect-document` to include raw block text in its output; the frontend provides a **Show full extracted text** checkbox. These explicit inspection options display sensitive contents and should be used in a suitable environment. The frontend's **PDF upload** screen invokes the same service.

The database identifies a run by document version, source hash and extraction configuration. Repeating a completed run returns its existing ID. Repeating a partial, failed, pending or interrupted run retries only failed or unfinished pages; completed and blank pages are retained. Changed source bytes or extractor configuration, including the detected Tesseract version, produce a separate run. Each page is saved in its own transaction, and processing status reflects the saved output and errors.

PostgreSQL advisory locks prevent concurrent attempts for the same run; a competing attempt returns `processing_in_progress`. The claim and page writes use the same database connection, and PostgreSQL releases the claim if the connection closes. An abrupt interruption can leave a run marked processing until the command is repeated, when an unclaimed run can resume. This policy requires a direct PostgreSQL connection or session pooling; transaction-pooling proxies cannot safely hold a session advisory lock.

Inspect all runs for an existing document (the output includes short text previews):

```bash
cd backend
python -m app.cli inspect-document --source-type policy --document-id <document-uuid>
```

## Week 2 document-ingestion evaluation

The synthetic fixtures, labeled regression runner, and measured results are described in [the Week 2 evaluation](docs/week-02-evaluation.md). The [architecture note](docs/week-02-architecture.md) covers routing, evidence, retries, concurrency, logging, and known limitations. The evaluation uses only fictional data and requires a separate migrated PostgreSQL database and real English Tesseract data.
