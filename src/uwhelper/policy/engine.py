"""Deterministic policy rule engine.

Guarantees (enforced here, independent of policy content):
* Missing / not-computable inputs  -> UNKNOWN (never PASS).
* Contradictory inputs             -> metric not computed -> UNKNOWN.
* Not-meaningful ratios            -> REVIEW_REQUIRED.
* Unverified / annualized / stale  -> a would-be PASS or FAIL becomes
  REVIEW_REQUIRED (the would-be result is kept as ``indicative_result``).
* Exceptions can only soften FAIL into REVIEW_REQUIRED, never into PASS.
* The overall outcome is never "approved".
"""

from __future__ import annotations

import operator as op
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from uwhelper.domain.enums import OverallStatus, RuleResult
from uwhelper.domain.money import dstr, round_to
from uwhelper.metrics.commercial import days_between
from uwhelper.metrics.types import QUALITY_FLAGS, AnalysisInputs, InputValue, MetricResult
from uwhelper.policy.schema import Condition, Policy, Rule

_OPS = {">=": op.ge, ">": op.gt, "<=": op.le, "<": op.lt, "==": op.eq, "!=": op.ne}
_TOKEN = re.compile(r"\{(observed|threshold|operator|metric|rule_id)\}")


@dataclass
class EvaluationContext:
    loan_type: str
    is_secured: bool
    metrics: dict[str, MetricResult]
    inputs: AnalysisInputs
    document_types_present: set[str] = field(default_factory=set)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    as_of: date = field(default_factory=date.today)
    guarantor_count: int = 0


@dataclass
class RuleEvaluation:
    rule_id: str
    rule_version: int
    description: str
    severity: str
    result: RuleResult
    explanation: str
    indicative_result: RuleResult | None = None
    subject: str | None = None
    observed_value: str | None = None
    operator: str | None = None
    threshold: str | None = None
    policy_source: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def requires_review(self) -> bool:
        return self.result in (RuleResult.FAIL, RuleResult.REVIEW_REQUIRED, RuleResult.UNKNOWN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id, "rule_version": self.rule_version, "description": self.description,
            "severity": self.severity, "result": str(self.result),
            "indicative_result": str(self.indicative_result) if self.indicative_result else None,
            "subject": self.subject, "observed_value": self.observed_value, "operator": self.operator,
            "threshold": self.threshold, "explanation": self.explanation, "requires_review": self.requires_review,
            "policy_source": self.policy_source, "evidence": self.evidence,
        }


def _render(template: str, values: dict[str, str]) -> str:
    return _TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def _subject_value(subject: str, ctx: EvaluationContext) -> Any:
    kind, name = subject.split(":", 1)
    if kind == "metric":
        m = ctx.metrics.get(name)
        return m.value if m is not None and m.computed else None
    if kind == "input":
        iv = ctx.inputs.values.get(name)
        return iv.value if iv is not None and iv.ok else None
    if kind == "terms":
        return getattr(ctx.inputs.terms, name, None)
    if kind == "case":
        if name == "loan_type":
            return ctx.loan_type
        if name == "is_secured":
            return ctx.is_secured
    return None


def _as_decimal(x: Any) -> Decimal | None:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, bool):
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None


def evaluate_condition(cond: Condition, ctx: EvaluationContext) -> bool | None:
    """True / False, or None when the subject is unknown."""
    val = _subject_value(cond.subject, ctx)
    if cond.op == "is_known":
        return val is not None
    if cond.op == "is_unknown":
        return val is None
    if val is None:
        return None
    if cond.op in ("in", "not_in"):
        options = cond.value if isinstance(cond.value, list) else [cond.value]
        hit = val in options or str(val) in [str(o) for o in options]
        return hit if cond.op == "in" else not hit
    left, right = _as_decimal(val), _as_decimal(cond.value)
    if left is not None and right is not None:
        return _OPS[cond.op](left, right)
    if cond.op in ("==", "!="):
        return _OPS[cond.op](str(val), str(cond.value))
    return None


def _describe(cond: Condition) -> str:
    return f"{cond.subject} {cond.op} {cond.value}"


def _evidence_from_inputs(inputs: list[InputValue]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for i in inputs:
        for s in i.sources:
            key = (s.get("document_id"), s.get("page_number"), i.label)
            if key in seen:
                continue
            seen.add(key)
            out.append({**s, "input": i.label})
    return out


def _quality_flags(inputs: list[InputValue]) -> set[str]:
    flags: set[str] = set()
    for i in inputs:
        flags |= i.flags & QUALITY_FLAGS
        if i.state == "ok" and not i.verified:
            flags.add("unverified")
    return flags


class PolicyEngine:
    def __init__(self, policy: Policy):
        self.policy = policy

    def evaluate(self, ctx: EvaluationContext) -> list[RuleEvaluation]:
        return [self.evaluate_rule(rule, ctx) for rule in self.policy.rules]

    def evaluate_rule(self, rule: Rule, ctx: EvaluationContext) -> RuleEvaluation:
        source = rule.source or f"{self.policy.name} v{self.policy.version}"
        ev = RuleEvaluation(rule.id, rule.version, rule.description, rule.severity, RuleResult.UNKNOWN, "",
                            policy_source=source)
        if rule.applies_to != "all" and ctx.loan_type not in rule.applies_to:
            ev.result = RuleResult.NOT_APPLICABLE
            ev.explanation = f"Rule applies to {', '.join(rule.applies_to)}; this case is {ctx.loan_type}."
            return ev
        for cond in rule.conditions:
            outcome = evaluate_condition(cond, ctx)
            if outcome is False:
                ev.result = RuleResult.NOT_APPLICABLE
                ev.explanation = f"Applicability condition not met: {_describe(cond)}."
                return ev
            if outcome is None:
                ev.result = RuleResult.UNKNOWN
                ev.explanation = f"Cannot determine whether this rule applies: {cond.subject} is unknown."
                return ev

        handler = getattr(self, f"_eval_{rule.type}")
        raw, explanation, flags, inputs = handler(rule, ctx, ev)
        ev.evidence = _evidence_from_inputs(inputs)
        ev.result = raw
        ev.explanation = explanation

        if raw == RuleResult.FAIL and rule.exceptions:
            for exc in rule.exceptions:
                outcomes = [evaluate_condition(c, ctx) for c in exc.when]
                if all(o is True for o in outcomes):
                    ev.indicative_result = RuleResult.FAIL
                    ev.result = RuleResult.REVIEW_REQUIRED
                    ev.explanation += f" Exception {exc.id} applies: {exc.explanation} (FAIL softened to REVIEW_REQUIRED.)"
                    break

        triggers = flags & set(rule.review_on)
        if ev.result in (RuleResult.PASS, RuleResult.FAIL) and triggers:
            ev.indicative_result = ev.result
            ev.result = RuleResult.REVIEW_REQUIRED
            ev.explanation += (
                f" Result held for review because inputs are {', '.join(sorted(triggers))}; "
                f"it would otherwise be {ev.indicative_result}."
            )
        if rule.explanation:
            ev.explanation += " Policy note: " + _render(rule.explanation, {
                "observed": ev.observed_value or "n/a", "threshold": ev.threshold or "n/a",
                "operator": ev.operator or "", "metric": rule.metric or "", "rule_id": rule.id})
        return ev

    # ----- rule types -------------------------------------------------------
    def _eval_metric_threshold(self, rule: Rule, ctx: EvaluationContext, ev: RuleEvaluation):
        m = ctx.metrics.get(rule.metric or "")
        ev.subject, ev.operator, ev.threshold = f"metric:{rule.metric}", rule.operator, dstr(rule.threshold)
        if m is None:
            return RuleResult.UNKNOWN, f"Metric {rule.metric} was not produced.", set(), []
        if m.status == "not_applicable":
            return RuleResult.NOT_APPLICABLE, f"{m.label} is not applicable: {m.explanation}", set(), m.inputs
        if m.status == "not_computable":
            return RuleResult.UNKNOWN, f"{m.label} could not be calculated. {m.explanation}", set(), m.inputs
        if m.status == "not_meaningful":
            return RuleResult.REVIEW_REQUIRED, f"{m.label} is not meaningful: {m.explanation}", set(), m.inputs
        value = m.value
        assert value is not None and rule.threshold is not None and rule.operator is not None
        compared = round_to(value, rule.round_to) if rule.round_to is not None else value
        ev.observed_value = dstr(compared)
        passed = _OPS[rule.operator](compared, rule.threshold)
        rounding = f" (rounded to {rule.round_to} dp from {dstr(value)})" if rule.round_to is not None else ""
        expl = (f"{m.label} = {dstr(compared)}{rounding}; requirement {rule.operator} {dstr(rule.threshold)} "
                f"{'met' if passed else 'NOT met'}. Formula: {m.formula}")
        flags = set(m.flags & QUALITY_FLAGS)
        if not m.verified:
            flags.add("unverified")
        return (RuleResult.PASS if passed else RuleResult.FAIL), expl, flags, m.inputs

    def _eval_inputs_present(self, rule: Rule, ctx: EvaluationContext, ev: RuleEvaluation):
        missing, conflicts, used = [], [], []
        for name in rule.required_inputs:
            if name in ctx.metrics:
                m = ctx.metrics[name]
                iv = m.as_input()
            else:
                iv = ctx.inputs.get(name)
            used.append(iv)
            if iv.state == "conflict":
                conflicts.append(iv.label)
            elif not iv.ok:
                missing.append(iv.label if iv.label != name else name)
        ev.subject = "inputs:" + ",".join(rule.required_inputs)
        if conflicts:
            return RuleResult.REVIEW_REQUIRED, f"Contradictory values for: {', '.join(conflicts)}.", set(), used
        if missing:
            return RuleResult.UNKNOWN, f"Missing (or not verified) inputs: {', '.join(missing)}.", set(), used
        return RuleResult.PASS, "All required inputs are present.", _quality_flags(used), used

    def _eval_documents_present(self, rule: Rule, ctx: EvaluationContext, ev: RuleEvaluation):
        missing = [d for d in rule.documents if d not in ctx.document_types_present]
        ev.subject = "documents:" + ",".join(rule.documents)
        if missing:
            return RuleResult.FAIL, f"Required document type(s) not on file: {', '.join(missing)}.", set(), []
        return RuleResult.PASS, "All required document types are on file (contents still require review).", set(), []

    def _eval_valuation_age(self, rule: Rule, ctx: EvaluationContext, ev: RuleEvaluation):
        ev.subject, ev.operator, ev.threshold = "collateral:valuation_age_days", "<=", str(rule.max_age_days)
        if not ctx.is_secured:
            return RuleResult.NOT_APPLICABLE, "Loan is unsecured.", set(), []
        if not ctx.inputs.collateral:
            return RuleResult.UNKNOWN, "No collateral recorded.", set(), []
        ages, unknown, stale = [], [], []
        for c in ctx.inputs.collateral:
            age = days_between(c.valuation_date, ctx.as_of)
            if age is None or not c.value.ok:
                unknown.append(c.description)
            else:
                ages.append(age)
                if age > (rule.max_age_days or 0):
                    stale.append(f"{c.description} ({age} days)")
        inputs = [c.value for c in ctx.inputs.collateral]
        if ages:
            ev.observed_value = str(max(ages))
        if stale:
            return RuleResult.FAIL, f"Valuation older than {rule.max_age_days} days: {'; '.join(stale)}.", set(), inputs
        if unknown:
            return RuleResult.UNKNOWN, f"Valuation date or value unknown for: {', '.join(unknown)}.", set(), inputs
        return RuleResult.PASS, f"All valuations are within {rule.max_age_days} days.", _quality_flags(inputs), inputs

    def _eval_statement_age(self, rule: Rule, ctx: EvaluationContext, ev: RuleEvaluation):
        ev.subject, ev.operator, ev.threshold = "financials:age_days", "<=", str(rule.max_age_days)
        age = days_between(ctx.inputs.period_end, ctx.as_of)
        if age is None:
            return RuleResult.UNKNOWN, "No base financial period end date.", set(), []
        ev.observed_value = str(age)
        if age > (rule.max_age_days or 0):
            return (RuleResult.FAIL, f"Base period ended {ctx.inputs.period_end} ({age} days ago), older than "
                    f"{rule.max_age_days} days.", set(), [])
        return RuleResult.PASS, f"Base period ended {ctx.inputs.period_end} ({age} days ago).", set(), []

    def _eval_no_contradictions(self, rule: Rule, ctx: EvaluationContext, ev: RuleEvaluation):
        ev.subject = "contradictions"
        ev.observed_value = str(len(ctx.contradictions))
        if ctx.contradictions:
            items = ", ".join(sorted({f"{c['field_key']} ({c.get('period_label') or 'n/a'})" for c in ctx.contradictions}))
            return RuleResult.REVIEW_REQUIRED, f"Unresolved contradictions between sources: {items}.", set(), []
        return RuleResult.PASS, "No unresolved contradictions between sources.", set(), []

    def _eval_guarantor_present(self, rule: Rule, ctx: EvaluationContext, ev: RuleEvaluation):
        ev.subject, ev.observed_value = "guarantors", str(ctx.guarantor_count)
        if ctx.guarantor_count > 0:
            return RuleResult.PASS, f"{ctx.guarantor_count} guarantor(s) recorded.", set(), []
        return RuleResult.FAIL, "No guarantor recorded.", set(), []

    def _eval_review_flag(self, rule: Rule, ctx: EvaluationContext, ev: RuleEvaluation):
        return RuleResult.REVIEW_REQUIRED, rule.description, set(), []


def overall_status(evaluations: list[RuleEvaluation], contradictions: list[dict], mode: str) -> OverallStatus:
    results = {e.result for e in evaluations}
    if RuleResult.UNKNOWN in results:
        return OverallStatus.INCOMPLETE
    if contradictions or mode != "verified_only":
        return OverallStatus.REVIEW_REQUIRED
    if results & {RuleResult.FAIL, RuleResult.REVIEW_REQUIRED}:
        return OverallStatus.REVIEW_REQUIRED
    return OverallStatus.NO_EXCEPTIONS_IDENTIFIED
