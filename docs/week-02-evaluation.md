# Week 2 synthetic PDF evaluation

## Scope

This is a small regression set, not a measurement of production OCR accuracy. All PDFs and labels are self-created, fictional English insurance examples without customer data. The committed files and their hashes are in `backend/fixtures/pdf/ground-truth.json`; `generate.py` contains their construction. The set consists of one digital policy, one image-only policy, one two-page mixed policy, one hybrid page with a scanned body and selectable header, one blank page, and one malformed PDF. The images are clean rasterizations of large, simple text; the mixed PDF repeats the same wording on its two pages.

The evaluator runs each file through the normal ingestion service against an existing synthetic policy-document record, then reads the persisted run and pages. It checks the document ID and policy version, PDF hash, page numbers, expected page count, and that available block boxes are within the recorded page dimensions. This checks source linkage and coordinate bounds; it does **not** prove that every box tightly highlights the correct word. The malformed PDF must produce `invalid_pdf` without creating a run.

## Measured results

The following run was made on 2026-11-27 using an isolated PostgreSQL database, Python 3.12.16, PyMuPDF 1.28.2, Tesseract 5.5.0 with English data, extractor version 3, 200 DPI, a 30-second native timeout, and a 60-second OCR timeout. The machine-readable output, fixture hashes, exact expected and extracted text, and effective configuration are in [week-02-evaluation.json](week-02-evaluation.json). Its run IDs were generated in the disposable database used for this measurement and are not expected to remain accessible.

| Measure | Result |
|---|---:|
| Native extraction coverage for expected digital pages | 2 / 2 (100%) |
| OCR fallback on text-bearing pages | 3 / 5 (60%); 3 / 6 including the blank page |
| Page route and outcome match, including blank | 6 / 6 |
| Scanned-page character error rate | 0 / 84 reference characters (0%) |
| Scanned-page word error rate | 0 / 14 reference words (0%) |
| Hybrid-page character error rate | 0 / 42 reference characters (0%) |
| Hybrid-page word error rate | 0 / 7 reference words (0%) |
| Exact policy-number matches | 5 / 5 |
| Exact insured-amount matches | 5 / 5 |
| Page provenance and coordinate-bound checks | 6 / 6 |
| Malformed PDF | Rejected as `invalid_pdf`; no run created |

Text is uppercased and whitespace-collapsed for scoring; stored raw text remains unchanged. CER and WER are total Levenshtein edit counts divided by reference characters and words respectively, across the two image-only OCR pages. The hybrid CER and WER measure the combined native and OCR output on the additional hybrid page. Field accuracy compares the extracted policy number and `EGP` amount with the labels on all five text-bearing pages. OCR fallback is the number of text-bearing pages routed to OCR or mixed, divided by all text-bearing pages.

The automated regression gate requires the expected page routes and provenance, exact labeled fields, rejection of the malformed input without a run, and scanned and hybrid CER no greater than 20%. That threshold is a fixture regression guard, not an acceptance threshold for real insurance documents.

## Reproduce with Docker

Use a **separate disposable database** and the default local Compose credentials. The `createdb` command fails if the chosen database already exists; choose another name instead of replacing existing data. The source document ID below is the stable fictional policy-document ID created by `fixtures.seed`.

```bash
docker compose up -d --wait db
docker compose exec -T db createdb -U claim week2_eval
docker compose build platform

docker compose run --rm \
  -e DATABASE_URL='postgresql+psycopg://claim:claim_local@db:5432/week2_eval' \
  platform alembic upgrade head

docker compose run --rm \
  -e DATABASE_URL='postgresql+psycopg://claim:claim_local@db:5432/week2_eval' \
  platform python -m fixtures.seed

mkdir -p /tmp/claim-week2-eval
docker compose run --rm \
  -e DATABASE_URL='postgresql+psycopg://claim:claim_local@db:5432/week2_eval' \
  -e PDF_STORAGE_DIR=/tmp/week2-eval-pdfs \
  -e PDF_OCR_LANGUAGE=eng -e PDF_OCR_DPI=200 \
  -e PDF_NATIVE_TIMEOUT_SECONDS=30 -e PDF_OCR_TIMEOUT_SECONDS=60 \
  -e PDF_OCR_MAX_PIXELS=25000000 \
  -v /tmp/claim-week2-eval:/reports \
  platform python -m app.document_evaluation \
    --document-id ae103f0e-3117-559f-a9e8-1ea3a8e8612c \
    --output /reports/results.json
```

The command writes synthetic PDFs to the temporary container storage and results to `/tmp/claim-week2-eval/results.json` on the host. It returns nonzero if the regression gate fails. The existing CLI can also demonstrate the mixed file end to end after seeding:

```bash
docker compose run --rm \
  -e DATABASE_URL='postgresql+psycopg://claim:claim_local@db:5432/week2_eval' \
  -e PDF_STORAGE_DIR=/tmp/week2-demo-pdfs \
  platform python -m app.cli ingest-pdf \
    --source-type policy \
    --document-id ae103f0e-3117-559f-a9e8-1ea3a8e8612c \
    --file fixtures/pdf/mixed-policy.pdf
```

The fixture hashes are checked before evaluation. Regenerating fixtures with a different PyMuPDF or compression version can change PDF bytes; review the generated content and update the manifest hashes if that happens. Tesseract or dependency versions can also change recognition results and the extraction configuration hash.

## Limitations

The set has six pages, only two image-only OCR pages and one hybrid page, one font, simple English text, no real claim data, and no independent layout or highlight annotation. It does not exercise handwriting, noisy scans, tables, Arabic, or the variety of insurance layouts. Perfect scores here demonstrate these fixtures passed on this configuration; they do not establish a global accuracy rate or suitability for production decisions. Page routing and text reconstruction remain heuristic, and user authorization is outside this local application slice. Table relationships are not reconstructed; policy tables should not be relied on in RAG until their relationships are captured and evaluated.
