# Week 1 — AI extraction evaluation

- Provider: gemini
- Model: `gemini-3.5-flash-lite`
- Prompt version: `claim-extraction-v1`
- Dataset: `week-01-v1` (12 synthetic examples)
- Run time: 2026-11-19T21:52:57.460768+00:00

## Method

Labels were authored by a separate project assistant before the evaluated model was called; they have not yet been independently reviewed by a human. Each claim was processed independently without a trusted policy association. Dates and incident types use their canonical schema values; policy references are trimmed; currencies are uppercase; amounts are compared as decimals. Damage parts are compared as unordered normalized sets using the explicit aliases in the runner. Missing fields are compared as exact sets. For ambiguity cases, the disputed field must be null and an uncertainty must contain a topic keyword. The expected uncertainty arrays are schema placeholders; `uncertainty_topic` defines the semantic expectation for ambiguous cases. This keyword test is a heuristic, not a semantic assessment. No calls modify claim records.

## Results

- Valid structured responses: **12/12**. Provider errors: `{}`.
- Correct missing-field sets: **12/12** valid responses.
- Ambiguity/contradiction handling: **3/3** valid ambiguity cases.
- Cases with potentially unsupported facts in the six scored fields: **0/12** valid responses; candidates need human review.
- Latency: mean 998.6 ms; range 867–1190 ms, including failures.
- Reported input tokens: 1845 across 12/12 cases; output tokens: 1030 across 12/12 cases.

### Field-level correctness

| Field | Correct / valid responses |
|---|---:|
| incident_type | 12 / 12 |
| incident_date | 12 / 12 |
| policy_number | 12 / 12 |
| damaged_parts | 12 / 12 |
| estimated_repair_cost | 12 / 12 |
| currency | 12 / 12 |

## Cases needing review

- None under the scoring rules above.

## Limitations and suggested improvements

This small synthetic dataset is a baseline, not evidence of production quality. Exact labels and the limited damage-part alias list can count valid synonyms as errors; review candidate unsupported facts and uncertainty wording manually. Free-text uncertainties are checked for relevant keywords on ambiguous cases but are not automatically screened for invented facts. Schema-valid output can still be factually wrong. Expand Arabic and English cases and independently review labels, synonyms, and ambiguity scoring before using these metrics to compare models. Provider failures are counted against schema validity but excluded from field and completeness denominators. Unknown token counts are excluded from totals, and latency includes network time. See the companion JSON report for individual results and errors.
