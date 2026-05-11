"""Compliance Firewall.

Scans AI-generated sales copy for RTO compliance violations. This is a
pattern-based safety net, not a substitute for human compliance review.

Hard violations (BANNED_PATTERNS) cause the output to be auto-rewritten
or blocked. Soft warnings (WARNING_PATTERNS) are surfaced for human review
but do not block display.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Tuple

# --- Hard blocks: phrases that always cause a violation ---
BANNED_PATTERNS: List[Tuple[str, str]] = [
    (r"\bguaranteed?\s+(?:job|jobs|employment|placement|outcome)",
     "Outcome guarantee — RTOs cannot guarantee employment"),
    (r"\b100\s*%?\s+(?:job|employment|placement|success|pass)\b",
     "Absolute outcome claim"),
    (r"\bcheapest\b",
     "Unsubstantiated comparative pricing claim"),
    (r"\blowest\s+(?:price|cost|fee|priced)\b",
     "Unsubstantiated comparative pricing claim"),
    (r"\bfree\s+training\b",
     "Misleading cost claim — use 'government-subsidised' or name the specific funding program"),
    (r"\bno[\s-]?cost\s+(?:course|training|qualification)\b",
     "Misleading cost claim"),
    (r"\bASQA[\s-]?(?:approved|endorsed|recommended)\b",
     "Implies ASQA endorsement of marketing — not permitted"),
]

# --- Soft warnings: surface to user but don't block ---
WARNING_PATTERNS: List[Tuple[str, str]] = [
    (r"\bbest\s+(?:RTO|training\s+provider|training\s+organisation)\b",
     "Unsubstantiated superlative"),
    (r"\b#\s*1\s+(?:RTO|provider|training)\b",
     "Unsubstantiated ranking claim"),
    (r"\bnationally\s+recognis(?:ed|ed)\b",
     "Verify the named course is actually on your scope of registration"),
    (r"\b(?:guaranteed|promised)\s+(?:salary|wage|pay)\b",
     "Salary outcome claim — must be substantiated"),
    (r"\bpass\s+rate\s+of\s+\d+",
     "Specific pass rate claim — must be evidenced and current"),
]


@dataclass
class ComplianceResult:
    """Result of a compliance scan."""

    cleared: bool
    violations: List[Tuple[str, str]] = field(default_factory=list)
    warnings: List[Tuple[str, str]] = field(default_factory=list)
    original_text: str = ""

    @property
    def status(self) -> str:
        if self.violations:
            return "BLOCKED"
        if self.warnings:
            return "WARNING"
        return "CLEARED"

    @property
    def summary(self) -> str:
        if self.violations:
            return f"BLOCKED — {len(self.violations)} hard violation(s)"
        if self.warnings:
            return f"WARNING — {len(self.warnings)} soft flag(s)"
        return "CLEARED"


def scan(text: str) -> ComplianceResult:
    """Run the firewall over a body of text. Case-insensitive."""
    if not text:
        return ComplianceResult(cleared=True, original_text="")

    violations: List[Tuple[str, str]] = []
    warnings: List[Tuple[str, str]] = []

    for pattern, reason in BANNED_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            violations.append((match.group(0), reason))

    for pattern, reason in WARNING_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            warnings.append((match.group(0), reason))

    return ComplianceResult(
        cleared=not violations,
        violations=violations,
        warnings=warnings,
        original_text=text,
    )


def scan_sequence(sequence: dict) -> ComplianceResult:
    """Convenience: flatten a sequence dict and scan all text fields."""
    parts: List[str] = []

    parts.append(sequence.get("linkedin_message", ""))

    email = sequence.get("email", {}) or {}
    parts.append(email.get("subject", ""))
    parts.append(email.get("body", ""))

    phone = sequence.get("phone_script", {}) or {}
    parts.append(phone.get("opener", ""))
    parts.append(phone.get("value_statement", ""))
    parts.extend(phone.get("discovery_questions", []) or [])
    parts.append(phone.get("objection_response", ""))

    parts.append(sequence.get("signal_summary", ""))
    parts.append(sequence.get("rationale", ""))

    return scan(" \n ".join(p for p in parts if p))
