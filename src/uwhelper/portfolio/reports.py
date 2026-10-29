"""Portfolio of funded deals: volume, count and commissions grouped by lender, referral source, product, deal type or
funding month, with filters and a CSV export of exactly the visible table.

* Funded amount and date: the deal's funding confirmation, else the linked file's funding (deals funded through
  their file), else the requested amount / status date (marked ``estimate``).
* Commissions: effective (non-reversed) ledger entries — expected and received by the broker, paid to partners.
* The CSV escapes cells that a spreadsheet would treat as a formula (leading = + - @, tab or CR).
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

GROUPS = {"lender": "Lender", "referral_source": "Referral source", "product": "Product", "deal_type": "Deal type",
          "month": "Funding month"}


def funded_deals(conn: sqlite3.Connection, *, date_from: date | None = None, date_to: date | None = None,
                 deal_type: str | None = None) -> list[dict[str, Any]]:
    from uwhelper.brokerage.fees import effective_entries

    rows = []
    for d in conn.execute(
            """SELECT d.*, rc.first_name || ' ' || rc.last_name AS referral_name, df.funded_amount AS deal_funded,
                      df.funding_date AS deal_funding_date, df.offer_id, cf.funded_amount AS case_funded,
                      cf.funding_date AS case_funding_date
               FROM deals d LEFT JOIN contacts rc ON rc.id=d.referral_source_contact_id
               LEFT JOIN deal_fundings df ON df.deal_id=d.id LEFT JOIN fundings cf ON cf.case_id=d.loan_case_id
               WHERE d.status IN ('FUNDED','SERVICING')"""):
        estimate = False
        if d["deal_funded"]:
            amount, fdate = Decimal(d["deal_funded"]), d["deal_funding_date"]
        elif d["case_funded"]:
            amount, fdate = Decimal(d["case_funded"]), d["case_funding_date"]
        else:
            amount, fdate, estimate = Decimal(d["requested_amount"] or 0), d["status_changed_at"][:10], True
        if date_from and fdate < date_from.isoformat():
            continue
        if date_to and fdate > date_to.isoformat():
            continue
        if deal_type and d["deal_type"] != deal_type:
            continue
        lender = None
        if d["offer_id"]:
            lender = conn.execute("""SELECT o.name FROM offers f JOIN submissions s ON s.id=f.submission_id
                                     JOIN lender_programs p ON p.id=s.program_id
                                     JOIN organizations o ON o.id=p.organization_id WHERE f.id=?""",
                                  (d["offer_id"],)).fetchone()[0]
        else:
            acc = conn.execute("""SELECT o.name FROM offers f JOIN submissions s ON s.id=f.submission_id
                                  JOIN lender_programs p ON p.id=s.program_id JOIN organizations o ON o.id=p.organization_id
                                  WHERE f.deal_id=? AND f.status='accepted'""", (d["id"],)).fetchone()
            lender = acc[0] if acc else None
        entries = effective_entries([dict(e) for e in conn.execute(
            "SELECT * FROM commission_entries WHERE deal_id=?", (d["id"],))])

        rows.append({"deal_id": d["id"], "deal_number": d["deal_number"], "name": d["name"], "deal_type": d["deal_type"],
                     "product": d["product_type"], "lender": lender or "(not recorded)",
                     "referral_source": (d["referral_name"] or "").strip() or "(none)", "funded_amount": amount,
                     "funding_date": fdate, "month": fdate[:7], "estimate": estimate,
                     "commission_expected": _total(entries, "expected_in"),
                     "commission_received": _total(entries, "received"), "paid_to_partners": _total(entries, "paid_out")})
    rows.sort(key=lambda r: (r["funding_date"], r["deal_number"]))
    return rows


def _total(entries: list[dict[str, Any]], entry_type: str) -> Decimal:
    return sum((Decimal(e["amount"]) for e in entries if e["entry_type"] == entry_type), Decimal(0))


def grouped(rows: list[dict[str, Any]], by: str) -> list[dict[str, Any]]:
    if by not in GROUPS:
        by = "lender"
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = out.setdefault(r[by], {"group": r[by], "deals": 0, "funded": Decimal(0), "commission_expected": Decimal(0),
                                   "commission_received": Decimal(0), "paid_to_partners": Decimal(0), "estimates": 0})
        g["deals"] += 1
        g["funded"] += r["funded_amount"]
        g["estimates"] += int(r["estimate"])
        for k in ("commission_expected", "commission_received", "paid_to_partners"):
            g[k] += r[k]
    res = sorted(out.values(), key=lambda g: (-g["funded"], str(g["group"])))
    for g in res:
        g["average"] = (g["funded"] / g["deals"]).quantize(Decimal("0.01")) if g["deals"] else Decimal(0)
    return res


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"deals": len(rows), "funded": sum((r["funded_amount"] for r in rows), Decimal(0)),
            "commission_expected": sum((r["commission_expected"] for r in rows), Decimal(0)),
            "commission_received": sum((r["commission_received"] for r in rows), Decimal(0)),
            "paid_to_partners": sum((r["paid_to_partners"] for r in rows), Decimal(0)),
            "estimates": sum(int(r["estimate"]) for r in rows)}


def _safe(v: Any) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def to_csv(groups: list[dict[str, Any]], by: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([GROUPS.get(by, "Group"), "Deals", "Funded amount", "Average", "Commission expected",
                "Commission received", "Paid to partners", "Estimated amounts"])
    for g in groups:
        w.writerow([_safe(g["group"]), g["deals"], g["funded"], g["average"], g["commission_expected"],
                    g["commission_received"], g["paid_to_partners"], g["estimates"]])
    return buf.getvalue()
