# Policies

Policies are YAML files in `policies/`, validated by `policy/schema.py` and registered on startup or via
*Policies → Reload*. **Demo policies are illustrative and must not be treated as lending standards.**

## Versioning
* `policy_key` + `version` (semver) identify a version. Registered versions are immutable (trigger + hash check).
* To change a rule: copy the file (e.g. `demo_commercial_general_v1.1.0.yaml`), bump `version`, reload.
* Runs record the exact policy version id; cases use the highest version unless pinned on the case form.

## Rule fields
| Field | Meaning |
|---|---|
| `id`, `version`, `description`, `severity` | identity and importance (`info`…`critical`) |
| `type` | `metric_threshold`, `inputs_present`, `documents_present`, `valuation_age`, `statement_age`, `no_contradictions`, `guarantor_present`, `review_flag` |
| `applies_to` | loan types or `all` (otherwise NOT_APPLICABLE) |
| `metric`, `operator`, `threshold`, `round_to` | deterministic comparison for `metric_threshold` |
| `required_inputs`, `documents`, `max_age_days` | parameters for the other types |
| `conditions` | `{subject, op, value}`; subject `metric:x`, `input:x`, `terms:x`, `case:loan_type/is_secured`. False → NOT_APPLICABLE, unknown → UNKNOWN |
| `exceptions` | documented cases that turn FAIL into REVIEW_REQUIRED (never PASS — enforced) |
| `review_on` | input qualities that hold a PASS/FAIL for review (default: unverified, annualized, stale, estimate, …) |
| `explanation`, `source`, `effective_date` | explanation template (`{observed}`, `{threshold}`), policy source |

## Results
`PASS`, `FAIL` (→ human review, not a denial), `UNKNOWN` (missing/uncomputable), `NOT_APPLICABLE`,
`REVIEW_REQUIRED` (unverified/stale/annualized inputs, not-meaningful ratios, contradictions, exceptions).
Overall: any UNKNOWN → INCOMPLETE; any FAIL/REVIEW, contradiction or preview run → REVIEW_REQUIRED;
otherwise NO_EXCEPTIONS_IDENTIFIED ("not a credit approval").

## SBA and other programs
SBA 7(a)/504 and private/hard-money are listed as **not implemented**. Implementing them requires a policy whose
`source_description` cites the specific, dated program document (e.g. the SOP version) and, likely, a new
module with program-specific fields (eligibility, guarantee %, fees).

## CRE parameters and versions (Milestone 3)
`parameters.cre_cap_rate` (0–1, e.g. 0.075) and `parameters.cre_stress_rate_bps` (0–2000) feed the CRE cross-check
metrics; when absent those metrics are UNKNOWN. `demo_cre` v1.2.0 adds them plus four rules and takes effect on
2026-11-01; until then v1.1.0 applies. A file can pin a version (policy key + version on the file).
**Policies → Compare two versions** shows header, parameter, rule and condition differences. Saved runs keep the
version they used, and **Traceability check** on a run recomputes it from its stored inputs and that version.
