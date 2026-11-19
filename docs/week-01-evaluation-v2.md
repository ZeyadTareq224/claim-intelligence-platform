# Week 1 — Expanded AI extraction evaluation

- Run: `1d51083e-1710-458e-aae9-28ecabffedcd`
- Provider: gemini
- Model: `gemini-3.5-flash-lite`
- Prompt version: `claim-extraction-v1`
- Dataset: `week-01-v2` (40 synthetic examples)
- Dataset SHA-256: `301b68a0b2bf7e9ebfeb0c70b599ca5b7609fa16d96822eed31e9c51106769fb`
- Label review: independent review self-attested by user (chat self-attestation) at 2026-11-19T22:32:48.960760+00:00; recorded by assistant based on user confirmation
- Run time: 2026-11-19T22:38:12.558005+00:00

## Method

Each claim was processed independently without a trusted policy association. Dates and incident types use canonical schema values; policy references are trimmed; currencies are uppercase; amounts are compared as decimals. Damage parts are unordered sets with explicit aliases. Missing fields are exact sets. For ambiguous cases, the disputed field must be unknown and an uncertainty must mention a topic keyword. This is a heuristic, not semantic judgment. Label evidence is a review aid, not a verdict about model output. A mismatch is an extraction error under the labels, not proof that a fact is unsupported. Every emitted scalar, damaged part, and free-text uncertainty is queued separately for human support review; null and empty values make no factual assertion. No calls modify claim records.

## Results

- Valid structured responses: **39/40**. Provider errors: `{'invalid_output': 1}`.
- Correct missing-field sets: **38/39** valid responses.
- Ambiguity heuristic passed: **12/12** valid ambiguity cases.
- Factual support review: **0/161** emitted facts reviewed. Unsupported-fact rate is not measured until facts are reviewed.
- Latency: mean 1022.0 ms; range 779–1437 ms.
- Reported input tokens: 5988 across 39/40 cases; output tokens: 3429 across 39/40 cases.

### Field-level correctness

| Field | Correct / valid responses |
|---|---:|
| incident_type | 38 / 39 |
| incident_date | 39 / 39 |
| policy_number | 39 / 39 |
| damaged_parts | 39 / 39 |
| estimated_repair_cost | 39 / 39 |
| currency | 37 / 39 |

## Cases needing review

- `v2-12` (English incident type unknown): field mismatches: incident_type; missing fields differ.
- `v2-16` (English contradictory estimate): field mismatches: currency.
- `v2-26` (Arabic repair cost EGP): provider error `invalid_output`.
- `v2-31` (Arabic contradictory amount): field mismatches: currency.

## Limitations and suggested improvements

This small synthetic dataset is a baseline, not evidence of production quality. Label review is self-attested; exact labels and a finite synonym map can mark valid paraphrases as mismatches. Review support independently using the full claim and source excerpts. Free-text uncertainty review and the keyword ambiguity check cannot be reduced to schema validity. Provider failures count against schema validity but are excluded from field and completeness denominators. Unknown usage is excluded from token totals. See the companion JSON for all individual outputs and reviews.
