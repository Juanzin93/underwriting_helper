"""Compare two versions of a policy: parameters, rules (added / removed / changed fields) and conditions."""

from __future__ import annotations

from typing import Any

from uwhelper.policy.schema import Policy


def _flat(d: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(d, dict):
        out: dict[str, Any] = {}
        for k, v in d.items():
            out.update(_flat(v, f"{prefix}{k}."))
        return out
    return {prefix.rstrip("."): d}


def _changes(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    fa, fb = _flat(a), _flat(b)
    return [{"field": k, "old": fa.get(k), "new": fb.get(k)} for k in sorted(set(fa) | set(fb))
            if str(fa.get(k)) != str(fb.get(k))]


def diff_policies(old: Policy, new: Policy) -> dict[str, Any]:
    o, n = old.model_dump(mode="json"), new.model_dump(mode="json")
    header = [c for c in _changes({k: o[k] for k in ("version", "name", "loan_types", "effective_date", "illustrative",
                                                      "source_description")},
                                  {k: n[k] for k in ("version", "name", "loan_types", "effective_date", "illustrative",
                                                      "source_description")})]

    def by(items: list[dict], key: str) -> dict[str, dict]:
        return {i[key]: i for i in items}

    orules, nrules = by(o["rules"], "id"), by(n["rules"], "id")
    ocond, ncond = by(o["conditions"], "code"), by(n["conditions"], "code")
    return {
        "old": f"{old.policy_key} v{old.version}", "new": f"{new.policy_key} v{new.version}",
        "header": header,
        "parameters": _changes(o["parameters"], n["parameters"]),
        "rules_added": [nrules[k] for k in nrules if k not in orules],
        "rules_removed": [orules[k] for k in orules if k not in nrules],
        "rules_changed": [{"id": k, "changes": _changes(orules[k], nrules[k])} for k in orules
                          if k in nrules and _changes(orules[k], nrules[k])],
        "conditions_added": [ncond[k] for k in ncond if k not in ocond],
        "conditions_removed": [ocond[k] for k in ocond if k not in ncond],
        "conditions_changed": [{"code": k, "changes": _changes(ocond[k], ncond[k])} for k in ocond
                               if k in ncond and _changes(ocond[k], ncond[k])],
    }
