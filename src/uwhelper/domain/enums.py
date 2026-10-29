"""Shared enumerations (string constants keep SQLite storage readable)."""

from __future__ import annotations

from enum import StrEnum


class RuleResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class FactStatus(StrEnum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class SourceType(StrEnum):
    EXTRACTED = "extracted"
    USER_ENTERED = "user_entered"
    USER_CORRECTED = "user_corrected"


class RunMode(StrEnum):
    VERIFIED_ONLY = "verified_only"
    INCLUDE_UNVERIFIED = "include_unverified"


class OverallStatus(StrEnum):
    INCOMPLETE = "INCOMPLETE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NO_EXCEPTIONS_IDENTIFIED = "NO_EXCEPTIONS_IDENTIFIED"


OVERALL_STATUS_TEXT = {
    OverallStatus.INCOMPLETE: "Incomplete — required information is missing, unverified or contradictory. No conclusion can be drawn.",
    OverallStatus.REVIEW_REQUIRED: "Review required — one or more screening rules failed or need human judgement. This is not a denial.",
    OverallStatus.NO_EXCEPTIONS_IDENTIFIED: (
        "Preliminary screen: no policy exceptions identified by the configured rules. "
        "THIS IS NOT A CREDIT APPROVAL — full human credit review is still required."
    ),
}

RESULT_SYMBOL = {
    "PASS": "✔ PASS",
    "FAIL": "✖ FAIL",
    "UNKNOWN": "? UNKNOWN",
    "NOT_APPLICABLE": "– N/A",
    "REVIEW_REQUIRED": "⚠ REVIEW",
}
