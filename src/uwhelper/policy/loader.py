"""Load, validate and register versioned policies.

* Files are parsed with ``yaml.safe_load`` (no arbitrary object construction).
* A (policy_key, version) pair is immutable: if a file's content changes but
  its version does not, loading fails with :class:`PolicyVersionConflict`.
* Every version ever loaded stays in the ``policies`` table so historical
  runs remain explainable.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from uwhelper.db.connection import Database, new_id, utcnow
from uwhelper.modules import get_module
from uwhelper.policy.schema import Policy


class PolicyError(ValueError):
    pass


class PolicyVersionConflict(PolicyError):
    pass


def canonical_json(policy: Policy) -> str:
    return json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def policy_sha256(policy: Policy) -> str:
    return hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest()


def parse_policy_text(text: str, *, source: str = "<string>") -> Policy:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"{source}: invalid YAML/JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"{source}: policy must be a mapping")
    try:
        policy = Policy.model_validate(data)
    except ValidationError as exc:
        raise PolicyError(f"{source}: {exc}") from exc
    validate_against_module(policy, source)
    return policy


def validate_against_module(policy: Policy, source: str = "") -> None:
    try:
        module = get_module(policy.module)
    except KeyError as exc:
        raise PolicyError(f"{source}: unknown module {policy.module!r}") from exc
    metrics = module.metric_catalog()
    fields = module.field_catalog()
    for lt in policy.loan_types:
        if lt not in module.loan_types:
            raise PolicyError(f"{source}: unknown loan type {lt!r}")
    for rule in policy.rules:
        if rule.metric and rule.metric not in metrics:
            raise PolicyError(f"{source}: rule {rule.id} references unknown metric {rule.metric!r}")
        for f in rule.required_inputs:
            if f not in fields and f not in metrics:
                raise PolicyError(f"{source}: rule {rule.id} references unknown input {f!r}")
        if rule.applies_to != "all":
            for lt in rule.applies_to:
                if lt not in module.loan_types:
                    raise PolicyError(f"{source}: rule {rule.id} applies to unknown loan type {lt!r}")
        for d in rule.documents:
            if d not in module.document_types():
                raise PolicyError(f"{source}: rule {rule.id} references unknown document type {d!r}")
        for cond in [*rule.conditions, *(c for e in rule.exceptions for c in e.when)]:
            kind, name = cond.subject.split(":", 1)
            if kind == "metric" and name not in metrics:
                raise PolicyError(f"{source}: rule {rule.id} condition uses unknown metric {name!r}")


def load_policy_file(path: Path) -> Policy:
    return parse_policy_text(path.read_text(encoding="utf-8"), source=path.name)


@dataclass
class LoadReport:
    loaded: list[str]
    unchanged: list[str]
    errors: list[str]


def register_policy(conn: sqlite3.Connection, policy: Policy, source_file: str | None = None) -> tuple[str, bool]:
    """Insert a policy version; returns (policy_id, created)."""
    digest = policy_sha256(policy)
    row = conn.execute(
        "SELECT id, content_sha256 FROM policies WHERE policy_key = ? AND version = ?",
        (policy.policy_key, policy.version),
    ).fetchone()
    if row:
        if row["content_sha256"] != digest:
            raise PolicyVersionConflict(
                f"Policy {policy.policy_key} v{policy.version} content changed but the version did not. "
                "Bump the version to publish a change; previous versions are immutable."
            )
        return row["id"], False
    pid = new_id()
    with Database.transaction(conn):
        conn.execute(
            """INSERT INTO policies(id, policy_key, version, name, module_key, loan_types_json, effective_date,
                   illustrative, source_description, source_file, content_sha256, content_json, loaded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, policy.policy_key, policy.version, policy.name, policy.module, json.dumps(policy.loan_types),
             policy.effective_date.isoformat(), int(policy.illustrative), policy.source_description, source_file,
             digest, canonical_json(policy), utcnow()),
        )
        conn.execute(
            "INSERT INTO audit_events(occurred_at, entity_type, entity_id, action, details_json) VALUES (?,?,?,?,?)",
            (utcnow(), "policy", pid, "policy_version_registered",
             json.dumps({"policy_key": policy.policy_key, "version": policy.version, "sha256": digest})),
        )
    return pid, True


def sync_policies_dir(conn: sqlite3.Connection, directory: Path) -> LoadReport:
    report = LoadReport([], [], [])
    if not directory.is_dir():
        report.errors.append(f"Policies directory not found: {directory}")
        return report
    for path in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml"), *directory.glob("*.json")]):
        try:
            policy = load_policy_file(path)
            _, created = register_policy(conn, policy, path.name)
            (report.loaded if created else report.unchanged).append(f"{policy.policy_key} v{policy.version}")
        except PolicyError as exc:
            report.errors.append(str(exc))
    return report


def policy_from_row(row: sqlite3.Row | dict) -> Policy:
    return Policy.model_validate(json.loads(row["content_json"]))


def list_policies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM policies ORDER BY policy_key, loaded_at").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["loan_types"] = json.loads(r["loan_types_json"])
        out.append(d)
    out.sort(key=lambda d: (d["policy_key"], tuple(int(x) for x in d["version"].split("."))))
    return out


def resolve_policy(conn: sqlite3.Connection, policy_key: str, version: str | None = None) -> tuple[str, Policy]:
    """Return (policy_id, Policy) for an exact version or the highest version."""
    if version:
        row = conn.execute("SELECT * FROM policies WHERE policy_key = ? AND version = ?", (policy_key, version)).fetchone()
        if not row:
            raise PolicyError(f"Policy {policy_key} v{version} is not registered")
        return row["id"], policy_from_row(row)
    rows = conn.execute("SELECT * FROM policies WHERE policy_key = ?", (policy_key,)).fetchall()
    if not rows:
        raise PolicyError(f"Policy {policy_key!r} is not registered")
    best = max(rows, key=lambda r: tuple(int(x) for x in r["version"].split(".")))
    return best["id"], policy_from_row(best)
