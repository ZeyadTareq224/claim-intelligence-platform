# Claim Intelligence Platform — Week 1

- **Author:** ZeyadTareq224
- **Date:** 2026-11-16

## Overview

Build a small, independent backend foundation for synthetic motor insurance claims in Egypt. The fixtures will include English and Arabic claim descriptions. A developer will be able to seed PostgreSQL, select a claim, extract facts using a configured language model, and save each analysis without changing the original claim.

The supplied Week 1 specification defines the required fields, scenarios, constraints, tests, and deliverables. The domain design is the first implementation step, not the whole Week 1 outcome. The AI extraction component will be a module in the same application and run in the same application service; PostgreSQL remains a separate database service.

## Analysis

The database needs eight entities: Organization, Vehicle, Policy, PolicyClause, Claim, ClaimDocument, PolicyDocument, and ClaimAnalysis. Relationships between organizations, vehicles, policies, and claims must be enforced in PostgreSQL; filtering in application code alone is insufficient. A claim may have no policy. Database consistency does not provide user authorization, which is out of scope.

Extraction facts, model uncertainties, and missing-information checks have different roles. The model extracts only supported facts; Python checks completeness. A trusted policy association may satisfy the policy-reference check even when the description contains no policy number. Analysis results are separate records and must not overwrite the claim or earlier analyses.

A model call must not hold a database transaction open. This creates a question about claims changing or being deleted during the call. The design must define what input and policy association an analysis represents and how to report a failed save. A privacy-conscious input fingerprint or snapshot is a possible solution; a fingerprint alone cannot reconstruct the text. Avoid storing sensitive provider errors or credentials.

The CLI needs clear errors for invalid input, missing configuration, provider failures, and database failures. Live extraction and the required evaluation need provider access, credentials, and budget; mocked tests do not. Results must distinguish tests from actual live evaluations.

## Goals vs. Non-Goals

**Goals**

- Implement the eight entities, migrations, relationships, and database constraints.
- Provide deterministic, repeatable synthetic data with the required scenarios and bilingual descriptions.
- Implement structured extraction, independent completeness checks, and append-only analysis history.
- Provide claim-ID and text-file CLI workflows, deterministic tests, and the specified live evaluation and report.
- Document setup, architecture, the domain model, and limitations.

**Non-goals**

- Odoo integration, HTTP APIs, authentication or authorization, document ingestion, OCR, RAG, automated decisions, payments, and deployment.
- Representing the synthetic examples as authoritative Egyptian insurance policy terms or legal advice.

## Alternatives Weighed

- **Application-only tenant checks:** insufficient because direct database writes could create cross-organization references. Composite foreign keys are a candidate for enforcing the required consistency.
- **Store extracted values directly on the claim:** rejected by the specification because unreviewed AI output must not overwrite original claim data.
- **Build a generic provider framework or future OCR/RAG infrastructure now:** unnecessary for Week 1. A small provider boundary and one official SDK adapter can support the current workflow.

## Key Decisions & Staging for Next Steps

- Start with the domain model and its relationship and constraint design, followed by migrations and database tests.
- Use Egypt as the synthetic setting and include English and Arabic descriptions; do not invent insurer-specific policy rules.
- Use short database transactions around reading and saving, with the provider call outside them.
- Keep ordinary seed reruns idempotent and preserve analysis history; define any local reset separately.
- Record model and prompt versions, errors, latency, and available token usage. Define error categories and latency boundaries during design.
- Resolve how analysis input and completeness context are identified when claims change; consider versioning the extraction schema and evaluation dataset for future comparisons.
- Before live evaluation, check provider access and document any inability to run it. Do not present mocked results as a live baseline.

## Open Questions

1. Should analysis history store an input snapshot, a fingerprint plus context, or another version reference? The choice affects privacy and reproducibility.
2. Which provider and model should be used for the live extraction and evaluation? The model must remain configurable.
3. How should concurrency and a failure to save after a successful provider call be surfaced to the CLI user?
