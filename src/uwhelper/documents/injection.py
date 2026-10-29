"""Heuristic detection of prompt-injection text inside documents.

Detection is a warning signal only. The real defences are structural:
document text is passed to the model as delimited data, the model has no
tools, its output is schema-validated, values must be found in the cited
page, and nothing extracted is trusted until a human verifies it.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore_instructions", re.compile(r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|all|earlier)\b.{0,40}\b(instructions?|prompts?|rules?|directions?)", re.I | re.S)),
    ("role_override", re.compile(r"\b(you are now|act as|pretend to be|new instructions?|system prompt|developer mode)\b", re.I)),
    ("chat_markup", re.compile(r"(<\|?(system|assistant|user|im_start|im_end)\|?>|\[/?INST\]|<<SYS>>|^\s*(system|assistant)\s*:)", re.I | re.M)),
    ("decision_steering", re.compile(r"\b(approve|approval of) (this|the) (loan|application|request)\b|\bmark (it|this|all) (as )?verified\b", re.I)),
    ("output_steering", re.compile(r"\b(respond|reply|output|return) (only )?(with|the following)\b.{0,30}\b(json|value|number)\b", re.I | re.S)),
    ("delimiter_spoof", re.compile(r"</?\s*document[_\-]", re.I)),
]


def scan_text(text: str) -> list[str]:
    if not text:
        return []
    return [name for name, pat in _PATTERNS if pat.search(text)]
