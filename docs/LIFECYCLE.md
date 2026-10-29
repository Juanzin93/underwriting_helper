# Loan lifecycle, decisions, conditions and closing

## Stages
`INTAKE → UNDERWRITING → CREDIT_REVIEW → STRUCTURING → DUE_DILIGENCE → CLOSING → FUNDED → SERVICING → PAID_OFF`
plus exception states `ON_HOLD`, `WITHDRAWN`, `DECLINED`, `CANCELLED`.

Every move is stored in `workflow_transitions` (append-only) with from/to stage, reason, time, actor and — for
overrides — the list of items that were overridden. Existing cases were placed in `INTAKE` by migration 0002.

## Transition rules
| From | Allowed targets |
|---|---|
| INTAKE | UNDERWRITING, ON_HOLD, WITHDRAWN, CANCELLED |
| UNDERWRITING | CREDIT_REVIEW, INTAKE, ON_HOLD, WITHDRAWN, CANCELLED |
| CREDIT_REVIEW | STRUCTURING, UNDERWRITING, DECLINED, ON_HOLD, WITHDRAWN, CANCELLED |
| STRUCTURING | DUE_DILIGENCE, CREDIT_REVIEW, UNDERWRITING, ON_HOLD, WITHDRAWN, CANCELLED |
| DUE_DILIGENCE | CLOSING, STRUCTURING, CREDIT_REVIEW, ON_HOLD, WITHDRAWN, CANCELLED |
| CLOSING | FUNDED (funding page only), DUE_DILIGENCE, CREDIT_REVIEW, ON_HOLD, WITHDRAWN, CANCELLED |
| FUNDED | SERVICING (opening the servicing account only) |
| SERVICING | PAID_OFF (servicing page, only with zero balance and nothing past due) |
| ON_HOLD | resume to the stage it was put on hold from, WITHDRAWN, CANCELLED |
| WITHDRAWN / DECLINED / CANCELLED | INTAKE (re-open, with a reason) |

All moves need a reason (≥ 10 characters). Forward moves run gate checks:

| Entering | ✖ Hard blockers (never bypassed) | ⚠ Soft items (override with ≥ 20-char reason) |
|---|---|---|
| UNDERWRITING | borrower legal name; requested amount | no documents uploaded |
| CREDIT_REVIEW | a **current** verified-only underwriting run | contradictions; unverified candidates; INCOMPLETE screening |
| STRUCTURING | latest human decision is an approval, based on the current run | no memo for that run |
| DECLINED | latest human decision is DECLINE | — |
| DUE_DILIGENCE | as above + human-approved terms recorded under the latest decision | no conditions on file |
| CLOSING | as above + all **required** due-diligence conditions SATISFIED or WAIVED | — |
| FUNDED | as above + required closing items closed; executed terms recorded; executed principal ≤ approved principal; typed confirmation phrase | executed terms differ from approved (rate, term…); closing record/date missing |

**Run currency.** A run is current only if rebuilding today's verified-only inputs gives the same SHA-256 and the
same policy version. Corrected or newly verified facts, revised requested terms, debt, collateral or guarantor
changes, or a new policy version make it stale. A stale run blocks forward moves and invalidates the decision based
on it (a new run and a new human decision are required).

## Term layers (never overwritten)
| Layer | Where | Who |
|---|---|---|
| Requested | `loan_terms` (revisioned) | user/borrower |
| Simulated | `loan_structures` (+ calculation snapshots) | user |
| Proposed | structure marked `proposed` | user |
| Approved | `term_sets` type `approved`, tied to a credit decision | responsible human |
| Executed | `term_sets` type `executed` | entered at closing |
| Funded | `fundings.funded_amount` | confirmed by a human |

## Credit decisions
`APPROVE`, `APPROVE_WITH_CONDITIONS`, `DECLINE`, `DEFER`; decided-by name, rationale, explicit "made by a human"
confirmation, linked run and latest memo, and a deterministic analysis of the approved terms (payment, highest annual
debt service, lowest-year DSCR, LTV). Decisions are immutable; a newer decision supersedes. Each line of approval
conditions becomes a required `DEC-nn` condition.

## Conditions (due diligence and closing)
Defined by the case policy (`conditions:` in policy v1.1.0+: code, description, category, phase, required, waivable,
responsible party, due days, applicability) or added manually / from the decision.
Status machine: `NOT_STARTED → REQUESTED → RECEIVED → UNDER_REVIEW → SATISFIED`, with `REJECTED` (needs a note),
re-opening a satisfied item (needs a reason) and `WAIVED`. SATISFIED requires a linked document or evidence note and
the reviewer's verification tick. A waiver requires the approver's name, an explanation (≥ 20 chars) and explicit
confirmation; items marked non-waivable (legal documents, beneficial ownership, lien searches, final review…) cannot
be waived. Every change is in `condition_events`.

## Closing and funding
The closing workspace records the closing date, disbursement instructions (stored locally; the audit log only notes
that they changed), a user-reported disbursement status, outstanding issues and the closing checklist. Funding is
recorded only after the user types `I CONFIRM THIS LOAN WAS FUNDED`; it cannot be dated in the future, cannot exceed the
executed principal, and is immutable. The application never executes agreements or moves money.

## Deals and underwriting files: one pipeline truth (Milestone 2)

A **deal** (Deals menu) owns the brokerage pipeline; its linked **underwriting file** owns the internal credit
lifecycle above. They stay consistent through recorded rules in `crm/pipeline.py` — every automatic change writes
history on both sides and an audit event (`deal_status_synced` / `workflow_transition`).

Deal statuses: INTAKE → UNDERWRITING → (CREDIT_REVIEW) → STRUCTURING → LENDER_SUBMISSION → APPROVED* → DUE_DILIGENCE
→ CLOSING → FUNDED* → SERVICING, plus ON_HOLD, WITHDRAWN, CANCELLED, DECLINED* (* named human + confirmation).
CREDIT_REVIEW is optional for a deal (a lender may do its own review).

| When the file … | the deal … |
|---|---|
| is funded (funding confirmation) | becomes FUNDED, authorized by the funding's "confirmed by" |
| opens servicing | becomes SERVICING |
| is declined (after a DECLINE credit decision) | becomes DECLINED, authorized by the decision's "decided by" |
| is withdrawn / cancelled | becomes WITHDRAWN / CANCELLED |

| When the deal … | the file … |
|---|---|
| is withdrawn / cancelled | is withdrawn / cancelled if not yet funded |
| is declined by lenders | is **not** changed; the deal page shows a notice |
| is marked funded by an outside lender | is **not** changed; the deal page explains that servicing here needs a confirmed funding on the file |

Guards: a file cannot be funded while its deal is declined/withdrawn/cancelled; a file cannot be declined/withdrawn/
cancelled while its deal is funded or servicing; a deal cannot be funded while its file is declined/withdrawn/cancelled.
A closed deal is never reopened automatically. An existing file can be linked to a deal from the deal page.

## Deal gates, closing and funding (Milestone 6)

| Deal moves to | Hard (must fix) | Soft (resolve, or override with a reason ≥ 20 characters) |
|---|---|---|
| DUE_DILIGENCE | — | no accepted lender offer |
| CLOSING | — | required due-diligence/closing conditions open; required due-diligence items not reviewed/waived; required document requests outstanding |
| FUNDED (funding confirmation only) | no closing record with a closing date; required conditions still open | due-diligence items outstanding; final terms or funded amount differ from the accepted offer; no accepted offer |

A deal becomes FUNDED only through **Closing & funding → Confirm funding** (typed phrase, amount, date, who), or
automatically when its linked underwriting file is funded. Overrides are written into the status history and audit.
