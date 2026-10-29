import sqlite3
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from tests.lifecycle_helpers import advance
from uwhelper.repo.cases import ValidationFailure, get_case
from uwhelper.servicing import ledger as LG
from uwhelper.servicing import monitoring as MO


@pytest.fixture
def acct(conn, settings):
    out = advance(conn, settings, "SERVICING", funding_days_ago=200)
    return out


def pay(conn, a, d, amount):
    return LG.record_payment(conn, a, LG.PaymentIn(received_date=d, amount=amount))


def test_schedule_from_executed_terms_and_funded_amount(conn, acct):
    a = acct["account_id"]
    sched = LG.schedules(conn, a)
    assert len(sched) == 1 and sched[0]["principal"] == "480000" and sched[0]["annual_rate"] == "0.0825"
    inst = LG.effective_installments(conn, a)
    assert len(inst) == 84 and inst[0]["payment"] == "7541.31"
    assert inst[0]["due_date"] == (acct["funding_date"] + timedelta(days=30)).isoformat()
    assert D(inst[-1]["balance_after"]) == 0
    assert get_case(conn, acct["case_id"])["stage"] == "SERVICING"


def test_no_payment_assumed_because_date_passed(conn, acct):
    st = LG.account_state(conn, acct["account_id"])
    assert st.outstanding_principal == D(480000) and st.total_received == 0
    assert st.past_due_count >= 6 and st.delinquency == "DPD_90_PLUS"


def test_payments_allocation_and_balance(conn, acct):
    a = acct["account_id"]
    inst = LG.effective_installments(conn, a)
    for i in inst[:3]:
        pay(conn, a, i["due_date"], i["payment"])
    st = LG.account_state(conn, a, date.fromisoformat(inst[2]["due_date"]))
    assert st.past_due_amount == 0 and st.delinquency == "CURRENT"
    expected_principal = sum(D(i["principal"]) for i in inst[:3])
    assert st.scheduled_principal_paid == expected_principal
    assert st.outstanding_principal == D(480000) - expected_principal
    assert st.outstanding_principal == D(inst[2]["balance_after"])
    assert st.interest_paid == sum(D(i["interest"]) for i in inst[:3])


def test_partial_payment_and_delinquency_buckets(conn, acct):
    a = acct["account_id"]
    inst = LG.effective_installments(conn, a)
    pay(conn, a, inst[0]["due_date"], "5000.00")  # partial: interest first
    as_of = date.fromisoformat(inst[0]["due_date"]) + timedelta(days=5)
    st = LG.account_state(conn, a, as_of)
    first = st.installments[0]
    assert first["status"] == "PARTIAL" and D(first["paid_interest"]) == D(inst[0]["interest"])
    assert st.days_past_due == 5 and st.delinquency == "WITHIN_GRACE"
    st = LG.account_state(conn, a, date.fromisoformat(inst[0]["due_date"]) + timedelta(days=45))
    assert st.delinquency == "DPD_30_59" and st.past_due_amount == D(inst[0]["payment"]) * 2 - D("5000.00")
    assert LG.bucket(95, 10) == "DPD_90_PLUS" and LG.bucket(0, 10) == "CURRENT" and LG.bucket(65, 10) == "DPD_60_89"


def test_prepayment_reduces_principal(conn, acct):
    a = acct["account_id"]
    inst = LG.effective_installments(conn, a)
    total_due = sum((D(i["payment"]) for i in inst if i["due_date"] <= date.today().isoformat()), D(0))
    payment_day = min(date.today(), date.fromisoformat(inst[0]["due_date"]) + timedelta(days=200))
    pay(conn, a, payment_day.isoformat(), str(total_due + D("10000.00")))
    st = LG.account_state(conn, a)
    assert st.past_due_amount == 0
    # extra money first covers future installments (paid ahead), so all principal paid is still traceable
    assert st.outstanding_principal == D(480000) - st.scheduled_principal_paid - st.unscheduled_principal_paid


def test_reversal_and_immutability(conn, acct):
    a = acct["account_id"]
    inst = LG.effective_installments(conn, a)
    pid = pay(conn, a, inst[0]["due_date"], inst[0]["payment"])
    with pytest.raises(ValidationFailure, match="reason"):
        LG.reverse_payment(conn, a, pid, "nsf")
    LG.reverse_payment(conn, a, pid, "Returned NSF by the bank")
    with pytest.raises(ValidationFailure, match="already"):
        LG.reverse_payment(conn, a, pid, "Returned NSF by the bank")
    assert LG.account_state(conn, a).total_received == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE payments SET amount='1' WHERE id=?", (pid,))


def test_payment_validation(conn, acct):
    a = acct["account_id"]
    with pytest.raises(ValidationFailure, match="before the funding"):
        pay(conn, a, (acct["funding_date"] - timedelta(days=1)).isoformat(), "100")
    with pytest.raises(ValidationFailure, match="future"):
        pay(conn, a, (date.today() + timedelta(days=1)).isoformat(), "100")
    with pytest.raises(Exception, match="whole cents"):
        LG.PaymentIn(received_date=date.today(), amount="10.005")
    with pytest.raises(Exception):
        LG.PaymentIn(received_date=date.today(), amount="-5")


def test_csv_import(conn, acct):
    a = acct["account_id"]
    inst = LG.effective_installments(conn, a)
    good = (f"date,amount,reference\n{inst[0]['due_date']},{inst[0]['payment']},CHK100\n"
            f"{inst[1]['due_date']},\"7,541.31\",CHK101\n").encode()
    res = LG.import_payments_csv(conn, a, good)
    assert res["imported"] == 2 and res["duplicates"] == 0
    assert LG.import_payments_csv(conn, a, good)["duplicates"] == 2
    bad = f"date,amount\n{inst[2]['due_date']},abc\nnot-a-date,100\n".encode()
    with pytest.raises(ValidationFailure, match="no rows saved"):
        LG.import_payments_csv(conn, a, bad)
    with pytest.raises(ValidationFailure, match="header"):
        LG.import_payments_csv(conn, a, b"foo,bar\n1,2\n")
    assert len([p for p in LG.payments(conn, a)]) == 2


def test_modification_creates_new_schedule_version(conn, acct):
    a = acct["account_id"]
    inst = LG.effective_installments(conn, a)
    paid = [i for i in inst if i["due_date"] < date.today().isoformat()]
    for i in paid:
        pay(conn, a, i["due_date"], i["payment"])
    eff = date.fromisoformat(paid[-1]["due_date"]) + timedelta(days=1)
    m = LG.ModificationIn(modification_type="rate_change", effective_date=eff, new_annual_rate="6.5",
                          new_first_payment_date=date.fromisoformat(inst[len(paid)]["due_date"]),
                          remaining_term_months=84 - len(paid), remaining_amortization_months=84 - len(paid),
                          reason="Rate reset per variable-rate addendum", approved_by="Credit Officer")
    LG.modify(conn, a, m)
    vers = LG.schedules(conn, a)
    assert [v["version"] for v in vers] == [1, 2] and vers[1]["annual_rate"] == "0.065" and vers[1]["is_current"]
    st = LG.account_state(conn, a)
    assert D(vers[1]["principal"]) == D(paid[-1]["balance_after"])
    assert st.past_due_amount == 0
    new_first = next(i for i in st.installments if i["schedule_version"] == 2)
    assert D(new_first["payment"]) < D(inst[0]["payment"])
    mods = LG.modifications(conn, a)
    assert mods[0]["before"]["annual_rate"] == "0.0825" and mods[0]["after"]["annual_rate"] == "0.065"


def test_modification_refused_with_arrears(conn, acct):
    a = acct["account_id"]
    with pytest.raises(ValidationFailure, match="unpaid"):
        LG.modify(conn, a, LG.ModificationIn(modification_type="term_extension", effective_date=date.today(),
                                             new_first_payment_date=date.today() + timedelta(days=30),
                                             remaining_term_months=60, remaining_amortization_months=60,
                                             reason="Extend the remaining term", approved_by="Officer"))


def test_covenants_tests_and_alerts(conn, acct):
    a = acct["account_id"]
    fd = acct["funding_date"]
    cid = MO.add_covenant(conn, a, MO.CovenantIn(code="MIN-DSCR", description="Minimum DSCR", metric_key="dscr",
                                                 operator=">=", threshold="1.25", first_test_date=fd - timedelta(days=400),
                                                 reporting_days=30))
    rows = MO.covenant_status(conn, a)
    assert rows[0]["missing"]  # an elapsed test period without a recorded test
    assert "COVENANT_TEST_MISSING" in {x["rule"] for x in MO.alerts(conn, a)}
    for pe in rows[0]["missing"]:
        MO.test_from_run(conn, a, cid, date.fromisoformat(pe["period_end"]), acct.get("run_id") or
                         conn.execute("SELECT id FROM underwriting_runs WHERE mode='verified_only' LIMIT 1").fetchone()[0])
    latest = MO.covenant_status(conn, a)[0]["latest"]
    assert latest["result"] == "PASS" and latest["observed_value"] == "1.639285"
    MO.test_manual(conn, a, cid, date.today(), "1.10", "Borrower interim statements reviewed")
    al = MO.alerts(conn, a)
    assert "COVENANT_BREACH" in {x["rule"] for x in al} and "METRIC_DETERIORATION" in {x["rule"] for x in al}
    with pytest.raises(ValidationFailure, match="evidence"):
        MO.test_manual(conn, a, cid, date.today(), "1.5", "")
    MO.test_manual(conn, a, cid, date.today(), "", "Statements not received; cannot measure")
    assert MO.covenant_status(conn, a)[0]["latest"]["result"] == "UNKNOWN"
    n = MO.refresh_events(conn, a)
    assert n >= 2 and MO.refresh_events(conn, a) == 0  # de-duplicated
    ev = MO.events(conn, a)[0]
    MO.acknowledge_event(conn, a, ev["id"], "Called borrower; statements due Friday")


def test_monitoring_items_recur(conn, acct):
    a = acct["account_id"]
    items = LG.monitoring_items(conn, a)
    fs = next(i for i in items if i["item_type"] == "financial_statements")
    nxt = LG.complete_monitoring_item(conn, a, fs["id"], "FY statements received and spread")
    assert nxt and len(LG.monitoring_items(conn, a)) == len(items) + 1


def test_maturity_and_balloon_alerts(conn, acct):
    a = acct["account_id"]
    st = LG.account_state(conn, a)
    mat = date.fromisoformat(st.maturity_date)
    rules = {x["rule"] for x in MO.alerts(conn, a, mat - timedelta(days=30))}
    assert "MATURITY_WITHIN_90_DAYS" in rules


def test_portfolio_totals(conn, acct):
    p = MO.portfolio(conn)
    assert p["totals"]["accounts"] == 1 and p["totals"]["outstanding"] == D(480000)
    assert p["totals"]["delinquent"] == 1 and p["by_type"]["equipment"] == D(480000)


def test_paid_off_requires_zero_balance(conn, acct):
    a = acct["account_id"]
    with pytest.raises(ValidationFailure, match="remain"):
        LG.mark_paid_off(conn, a, "Officer", "done")


def test_what_if(conn, acct):
    w = LG.what_if(conn, acct["account_id"], rate=D("0.10"), extra_payment=D(0), cfads=D(259000),
                   existing_other_ds=D(67500))
    assert D(w["level_payment"]) > D("7541.31") and w["dscr_minimum"]
