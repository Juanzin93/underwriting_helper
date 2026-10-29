"""Shared fixtures. All data is synthetic; no test needs Ollama or the internet."""

from __future__ import annotations

import ipaddress
import shutil
import socket
from decimal import Decimal

import pytest

from uwhelper.bootstrap import initialise
from uwhelper.config import load_settings
from uwhelper.domain.models import BorrowerIn, CaseIn, FactIn, TermsIn
from uwhelper.metrics.types import AnalysisInputs, InputValue, LoanTermsInput
from uwhelper.repo import cases, facts

HAS_TESSERACT = shutil.which("tesseract") is not None


class ExternalNetworkBlocked(RuntimeError):
    pass


def _is_loopback(host) -> bool:
    if host in ("localhost", "testserver"):
        return True
    try:
        return ipaddress.ip_address(str(host).strip("[]")).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    """Every test runs with external network access blocked: only loopback connections are allowed."""
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(host):
            raise ExternalNetworkBlocked(f"test attempted an external connection to {host!r}")
        return real_connect(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host is not None and not _is_loopback(host):
            raise ExternalNetworkBlocked(f"test attempted DNS resolution of {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield

# Known-answer income statement (annual).
IS_VALUES = dict(revenue="2400000", cost_of_goods_sold="1440000", operating_expenses="600000", net_income="230000",
                 interest_expense="40000", depreciation="60000", amortization="0", income_tax_expense="30000",
                 unfinanced_capex="25000", cash_taxes_paid="30000", distributions="50000")
BS_VALUES = dict(cash="150000", accounts_receivable="220000", current_assets="520000", current_liabilities="300000",
                 total_assets="1400000", total_liabilities="800000")


@pytest.fixture
def settings(tmp_path):
    return load_settings(data_dir=tmp_path / "data")


@pytest.fixture
def db(settings):
    database, report = initialise(settings)
    assert not report.errors
    return database


@pytest.fixture
def conn(db):
    with db.session() as c:
        yield c


def make_case(conn, loan_type="equipment", amount="500000", rate="8.5", amort=84, project_cost="625000", secured=True):
    return cases.create_case(
        conn, CaseIn(title="Test", loan_type=loan_type), BorrowerIn(legal_name="Synthetic Test Co"),
        TermsIn(requested_amount=amount, interest_rate=rate, amortization_months=amort, term_months=amort,
                project_total_cost=project_cost, is_secured=secured))


def add_verified_financials(conn, case_id, period_end="2025-12-31", is_values=None, bs_values=None):
    for k, v in (is_values or IS_VALUES).items():
        facts.add_user_fact(conn, case_id, FactIn(field_key=k, value=v, period_end=period_end, period_months=12,
                                                  mark_verified=True))
    for k, v in (bs_values or BS_VALUES).items():
        facts.add_user_fact(conn, case_id, FactIn(field_key=k, value=v, period_end=period_end, mark_verified=True))


@pytest.fixture
def full_case(conn):
    from uwhelper.domain.models import CollateralIn, ValuationIn

    cid = make_case(conn)
    add_verified_financials(conn, cid)
    cases.set_debt_schedule_confirmed(conn, cid, True)
    cases.add_collateral(conn, cid, CollateralIn(description="CNC", collateral_type="equipment"),
                         ValuationIn(value="900000", valuation_date="2026-06-01", valuation_method="appraisal",
                                     verification_status="verified"))
    return cid


def iv(name, value, verified=True, flags=None):
    return InputValue(name, name, Decimal(str(value)), "ok", verified, set(flags or ()))


def base_inputs(loan_type="equipment", **overrides) -> AnalysisInputs:
    vals = {k: iv(k, v) for k, v in IS_VALUES.items()}
    vals.update({k: iv(k, v) for k, v in BS_VALUES.items()})
    for k, v in overrides.items():
        if v is None:
            vals.pop(k, None)
        else:
            vals[k] = v if isinstance(v, InputValue) else iv(k, v)
    return AnalysisInputs(loan_type=loan_type, values=vals, debt_schedule_confirmed=True, period_end="2025-12-31",
                          terms=LoanTermsInput(Decimal(500000), Decimal("0.085"), 84, 84,
                                               project_total_cost=Decimal(625000)))
