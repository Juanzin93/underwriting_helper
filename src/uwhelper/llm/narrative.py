"""Optional model-generated narrative for a report — clearly labelled and checked.

Only structured run results (no document text) are sent to the local model.
Afterwards, every number in the narrative is checked against the numbers in
the input; unsupported numbers and decision language are flagged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from uwhelper.llm.client import OllamaClient, OllamaError
from uwhelper.llm.extraction import numbers_in_text

NARRATIVE_SYSTEM = """You write a short, neutral summary of a preliminary commercial credit screening for a human
credit analyst. Use ONLY the facts and numbers in the JSON provided. Do not introduce new numbers, do not
estimate, do not speculate about causes. Never recommend approval or denial and never say the loan is
approved or denied. Mention missing, unverified or contradictory information explicitly. 3 short paragraphs,
plain text, no headings."""

_DECISION = re.compile(r"\b(approve[ds]?|approval|den(y|ied|ial)|declin(e|ed)|reject(ed)?)\b", re.I)


@dataclass
class Narrative:
    text: str
    model: str
    unsupported_numbers: list[str] = field(default_factory=list)
    decision_language: list[str] = field(default_factory=list)
    error: str | None = None


def build_payload(run: dict[str, Any]) -> dict[str, Any]:
    metrics = {m["metric_key"]: {"label": m["label"], "value": m["value"], "status": m["status"]}
               for m in run["metrics"] if m["status"] != "not_applicable"}
    rules = [{"rule": e["rule_id"], "result": e["result"], "description": e["description"],
              "observed": e["observed_value"], "threshold": e["threshold"]} for e in run["evaluations"]
             if e["result"] != "NOT_APPLICABLE"]
    findings = [{"type": f["finding_type"], "message": f["message"]} for f in run["findings"]]
    return {"overall_status": run["overall_status"], "base_period": run["base_period_label"],
            "metrics": metrics, "rules": rules, "findings": findings}


def check_narrative(text: str, payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    allowed = numbers_in_text(json.dumps(payload))
    allowed_rounded = set()
    for d in allowed:
        for places in (0, 1, 2):
            q = Decimal(1).scaleb(-places)
            allowed_rounded.add(d.quantize(q))
            allowed_rounded.add((d * 100).quantize(q))  # ratios shown as percentages
    unsupported = []
    for tok in re.findall(r"\$?\d[\d,]*(?:\.\d+)?%?x?", text):
        clean = tok.replace("$", "").replace(",", "").rstrip("%x")
        if not clean or clean.isdigit() and len(clean) <= 1:
            continue
        try:
            d = Decimal(clean)
        except Exception:
            continue
        if d in allowed or any(d == r for r in allowed_rounded):
            continue
        unsupported.append(tok)
    decisions = sorted({m.group(0).lower() for m in _DECISION.finditer(text)})
    return unsupported, decisions


def generate_narrative(client: OllamaClient, run: dict[str, Any]) -> Narrative:
    payload = build_payload(run)
    try:
        client.ensure_local_model()
        text = client.chat_text(NARRATIVE_SYSTEM, "Screening results (JSON):\n" + json.dumps(payload, indent=1))
    except OllamaError as exc:
        return Narrative("", client.model, error=f"{exc.code}: {exc}")
    text = text.strip()[:4000]
    unsupported, decisions = check_narrative(text, payload)
    return Narrative(text, client.model, unsupported, decisions)
