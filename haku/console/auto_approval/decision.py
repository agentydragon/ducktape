"""The decision vocabulary every auto-approval policy evaluator returns.

Kept separate from `registry.py` (the registry/dispatch) so per-policy-kind evaluator modules
(`github.py`, `gmail.py`, `kubernetes.py`) can construct decisions without importing back from the
module that imports them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AutoDenied:
    reason: str
    evaluation: str


@dataclass(frozen=True, slots=True)
class AutoApproved:
    explanation: str


@dataclass(frozen=True, slots=True)
class NotAutoApproved:
    reason: str


type AutoApprovalDecision = AutoApproved | NotAutoApproved | AutoDenied
