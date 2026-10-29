from datetime import date
from decimal import Decimal as D

import pytest
import yaml

from tests.conftest import base_inputs, iv
from uwhelper.config import PROJECT_ROOT
from uwhelper.domain.enums import OverallStatus, RuleResult
from uwhelper.metrics.types import InputValue
from uwhelper.modules import get_module
from uwhelper.policy.engine import EvaluationContext, PolicyEngine, overall_status
from uwhelper.policy.loader import (
    PolicyError,
    PolicyVersionConflict,
    parse_policy_text,
    register_policy,
    resolve_policy,
    sync_policies_dir,
)

M = get_module("commercial")
POLICY = {
    "policy_key": "test_policy", "version": "1.0.0", "name": "Test", "module": "commercial",
    "loan_types": ["equipment", "general_commercial"], "effective_date": "2026-01-01", "illustrative": True,
    "disclaimer": "Illustrative only.", "source_description": "unit test",
    "parameters": {"collateral_advance_rates": {"equipment": 0.6}},
    "rules": [{"id": "DSCR_MIN", "version": 1, "description": "DSCR", "type": "metric_threshold", "metric": "dscr",
               "operator": ">=", "threshold": 1.25, "round_to": 2, "severity": "high"}],
}


def policy(**rule_overrides):
    data = yaml.safe_load(yaml.safe_dump(POLICY))
    data["rules"][0].update(rule_overrides)
    return parse_policy_text(yaml.safe_dump(data))


def ctx(inputs=None, **kw):
    inputs = inputs or base_inputs()
    metrics = M.compute_metrics(inputs, {"collateral_advance_rates": {"equipment": D("0.6")}})
    return EvaluationContext(inputs.loan_type, inputs.terms.is_secured, metrics, inputs, as_of=date(2026, 6, 1), **kw)


def evaluate(p, c):
    return PolicyEngine(p).evaluate(c)[0]


def test_pass_and_fail():
    assert evaluate(policy(), ctx()).result == RuleResult.PASS
    assert evaluate(policy(threshold=3), ctx()).result == RuleResult.FAIL




def test_boundary_rounding():
    p = policy(metric="current_ratio", threshold=1.25)
    # current ratio = CA / 1,000,000
    def cr(ca):
        return evaluate(p, ctx(base_inputs(current_assets=ca, current_liabilities=1000000))).result
    assert cr(1250000) == RuleResult.PASS  # exactly at threshold (>=)
    assert cr(1245000) == RuleResult.PASS  # 1.245 rounds half-up to 1.25
    assert cr(1244900) == RuleResult.FAIL  # 1.2449 rounds to 1.24
    unrounded = policy(metric="current_ratio", threshold=1.25, round_to=None)
    assert evaluate(unrounded, ctx(base_inputs(current_assets=1245000, current_liabilities=1000000))).result == RuleResult.FAIL


def test_missing_input_is_unknown_never_pass():
    e = evaluate(policy(), ctx(base_inputs(net_income=None)))
    assert e.result == RuleResult.UNKNOWN and e.requires_review


def test_unverified_input_holds_for_review():
    e = evaluate(policy(), ctx(base_inputs(net_income=iv("net_income", 230000, verified=False))))
    assert e.result == RuleResult.REVIEW_REQUIRED and e.indicative_result == RuleResult.PASS
    e = evaluate(policy(threshold=5), ctx(base_inputs(net_income=iv("net_income", 230000, verified=False))))
    assert e.result == RuleResult.REVIEW_REQUIRED and e.indicative_result == RuleResult.FAIL


def test_annualized_input_holds_for_review():
    e = evaluate(policy(), ctx(base_inputs(net_income=iv("net_income", 230000, flags={"annualized"}))))
    assert e.result == RuleResult.REVIEW_REQUIRED


def test_not_meaningful_is_review():
    p = policy(metric="debt_to_equity", operator="<=", threshold=4)
    e = evaluate(p, ctx(base_inputs(total_liabilities=2000000)))
    assert e.result == RuleResult.REVIEW_REQUIRED


def test_not_applicable_by_loan_type_and_condition():
    assert evaluate(policy(applies_to=["working_capital"]), ctx()).result == RuleResult.NOT_APPLICABLE
    p = policy(conditions=[{"subject": "terms:requested_amount", "op": ">", "value": 1000000}])
    assert evaluate(p, ctx()).result == RuleResult.NOT_APPLICABLE
    p = policy(conditions=[{"subject": "metric:global_dscr", "op": ">", "value": 1}])
    assert evaluate(p, ctx()).result == RuleResult.UNKNOWN  # condition subject unknown


def test_exception_softens_fail_to_review_only():
    p = policy(threshold=3, exceptions=[{"id": "EXC", "when": [{"subject": "metric:dscr", "op": ">=", "value": 2}],
                                        "explanation": "close enough"}])
    e = evaluate(p, ctx())
    assert e.result == RuleResult.REVIEW_REQUIRED and e.indicative_result == RuleResult.FAIL
    with pytest.raises(PolicyError):
        policy(exceptions=[{"id": "EXC", "when": [{"subject": "metric:dscr", "op": ">=", "value": 2}],
                            "result": "PASS", "explanation": "x"}])


def test_documents_contradictions_guarantor_age_rules():
    p = policy(type="documents_present", documents=["business_tax_return"], metric=None, operator=None, threshold=None)
    assert evaluate(p, ctx()).result == RuleResult.FAIL
    assert evaluate(p, ctx(document_types_present={"business_tax_return"})).result == RuleResult.PASS
    p = policy(type="no_contradictions", metric=None, operator=None, threshold=None)
    assert evaluate(p, ctx(contradictions=[{"field_key": "revenue", "period_label": "FY"}])).result == RuleResult.REVIEW_REQUIRED
    p = policy(type="guarantor_present", metric=None, operator=None, threshold=None)
    assert evaluate(p, ctx()).result == RuleResult.FAIL
    p = policy(type="statement_age", max_age_days=100, metric=None, operator=None, threshold=None)
    assert evaluate(p, ctx()).result == RuleResult.FAIL  # 2025-12-31 -> 2026-06-01 is 152 days
    p = policy(type="valuation_age", max_age_days=365, metric=None, operator=None, threshold=None)
    assert evaluate(p, ctx()).result == RuleResult.UNKNOWN  # secured but no collateral


def test_inputs_present_rule():
    p = policy(type="inputs_present", required_inputs=["revenue", "net_income"], metric=None, operator=None, threshold=None)
    assert evaluate(p, ctx()).result == RuleResult.PASS
    assert evaluate(p, ctx(base_inputs(net_income=None))).result == RuleResult.UNKNOWN
    conflict = InputValue("revenue", "Revenue", None, "conflict", False, {"conflict"})
    assert evaluate(p, ctx(base_inputs(revenue=conflict))).result in (RuleResult.UNKNOWN, RuleResult.REVIEW_REQUIRED)


def test_overall_status_never_approval():
    p = policy()
    evals = PolicyEngine(p).evaluate(ctx())
    assert overall_status(evals, [], "verified_only") == OverallStatus.NO_EXCEPTIONS_IDENTIFIED
    assert overall_status(evals, [], "include_unverified") == OverallStatus.REVIEW_REQUIRED
    assert overall_status(evals, [{"field_key": "x"}], "verified_only") == OverallStatus.REVIEW_REQUIRED
    assert "NOT A CREDIT APPROVAL" in __import__("uwhelper.domain.enums", fromlist=["x"]).OVERALL_STATUS_TEXT[
        OverallStatus.NO_EXCEPTIONS_IDENTIFIED]


@pytest.mark.parametrize("mutation,msg", [
    (lambda d: d["rules"][0].update(metric="no_such_metric"), "unknown metric"),
    (lambda d: d.update(version="1.0"), "semantic"),
    (lambda d: d["rules"].append(dict(d["rules"][0])), "duplicate"),
    (lambda d: d.update(disclaimer="none"), "illustrative"),
    (lambda d: d["rules"][0].update(unexpected=1), "Extra inputs"),
    (lambda d: d.update(loan_types=["spaceship"]), "unknown loan type"),
])
def test_policy_validation_errors(mutation, msg):
    data = yaml.safe_load(yaml.safe_dump(POLICY))
    mutation(data)
    with pytest.raises(PolicyError, match=msg):
        parse_policy_text(yaml.safe_dump(data))


def test_yaml_safe_load_blocks_objects():
    with pytest.raises(PolicyError):
        parse_policy_text("!!python/object/apply:os.system ['echo hi']")


def test_shipped_policies_valid_and_illustrative():
    for path in (PROJECT_ROOT / "policies").glob("*.yaml"):
        p = parse_policy_text(path.read_text(encoding="utf-8"))
        assert p.illustrative and "illustrative" in p.disclaimer.lower()


def test_policy_version_immutability(conn):
    p1 = policy()
    pid, created = register_policy(conn, p1)
    assert created and register_policy(conn, p1) == (pid, False)
    changed = policy(threshold=1.5)  # same version, different content
    with pytest.raises(PolicyVersionConflict):
        register_policy(conn, changed)
    data = yaml.safe_load(yaml.safe_dump(POLICY))
    data["version"] = "1.1.0"
    data["rules"][0]["threshold"] = 1.5
    pid2, _ = register_policy(conn, parse_policy_text(yaml.safe_dump(data)))
    latest_id, latest = resolve_policy(conn, "test_policy")
    assert latest_id == pid2 and latest.rules[0].threshold == D("1.5")
    old_id, old = resolve_policy(conn, "test_policy", "1.0.0")
    assert old_id == pid and old.rules[0].threshold == D("1.25")
    with pytest.raises(Exception):
        conn.execute("UPDATE policies SET content_json='{}' WHERE id=?", (pid,))


def test_sync_reports_bad_files(conn, tmp_path):
    (tmp_path / "bad.yaml").write_text("policy_key: x\n", encoding="utf-8")
    report = sync_policies_dir(conn, tmp_path)
    assert report.errors and not report.loaded
