"""Deterministic pre-classifier lint: reject prompts carrying credential material.

This is the cheap, zero-false-negative layer in front of the LLM classifier —
it catches the specific catastrophic case (a secret pasted into a prompt bound
for an external provider) with patterns, not judgment. Judgment calls (personal
context, PII) belong to the classifier.
"""

import re

# Pattern → human-readable name, surfaced to L0 in the rejection reason.
_CREDENTIAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"), "Anthropic API key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "API secret key (sk-…)"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), "GitLab token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"\bAGE-SECRET-KEY-1[A-Z0-9]{50,}"), "age secret key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM private key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\."), "JWT"),
]


def find_credentials(prompt: str) -> list[str]:
    """Names of credential kinds found in the prompt, empty when clean."""
    return [name for pattern, name in _CREDENTIAL_PATTERNS if pattern.search(prompt)]
