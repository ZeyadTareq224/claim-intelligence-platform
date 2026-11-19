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

`docker compose down -v` permanently deletes the Compose database volume, including any local data and analyses. To recreate an empty local database and its fixtures, run that command only when the data is disposable, then repeat the setup steps above.

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

The platform container connects to PostgreSQL at `db:5432` on the Compose network. If host port 5432 is occupied, export `POSTGRES_PORT=55433` before running the Compose commands. The host connection URL must then use port 55433; the container connection remains on port 5432. Keep the same `POSTGRES_PORT` value for subsequent Compose commands. Open http://localhost:8501 to view and manage the eight entity types. The UI has no authentication and is intended for local use with synthetic data; the port binds to localhost. The database enforces constraints, and deleting a referenced record may fail. Administration commands run in short-lived containers; the application does not apply migrations automatically. The AI module and any later API run in this same application service.

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

The independently labeled dataset is in `backend/app/ai/evaluation/dataset.json`. The runner uses the configured `AI_PROVIDER` and evaluates each description without saving analyses to the claims database. Live runs consume provider quota and may incur charges on paid projects.

After building the application image, run from the project root:

```bash
docker compose run --rm -v "$PWD/docs:/opt/claim-platform/docs" platform \
  python -m app.ai.evaluation --interval-seconds 6
```

The interval reduces the chance of hitting free-tier rate limits; it does not guarantee quota availability. The command writes `docs/week-01-evaluation.md` and `docs/week-01-evaluation.json`, replacing previous reports. The Markdown report describes the scoring and limitations, and the JSON includes case-level outputs and errors. Use only synthetic data on Gemini's free tier.
