"""Auto-approval predicate for the approval gate.

The predicate is a Python file loaded at startup that exports a function:

    def decide(server_namespace: str, tool_name: str, arguments: dict) -> Approved | Denied | NeedsHumanDecision:
        ...

Three outcomes:
  Approved()             — execute immediately, skip the queue
  Denied(reason=...)     — reject immediately, surface error to agent
  NeedsHumanDecision()   — queue for operator (default)

On load failure or runtime exception, the gate falls back to NeedsHumanDecision (fail-safe).
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


# ── Decision types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Approved:
    """Auto-approve: execute immediately without queuing."""


@dataclass(frozen=True)
class Denied:
    """Auto-deny: reject immediately, return reason to agent."""

    reason: str | None = None


@dataclass(frozen=True)
class NeedsHumanDecision:
    """Queue for operator approval (the safe default)."""


PredicateDecision = Approved | Denied | NeedsHumanDecision


# ── Predicate protocol ────────────────────────────────────────────────────────


class PredicateFn(Protocol):
    def __call__(self, server_namespace: str, tool_name: str, arguments: dict) -> PredicateDecision: ...


# ── Loader ────────────────────────────────────────────────────────────────────


def _always_needs_human(server_namespace: str, tool_name: str, arguments: dict) -> PredicateDecision:
    return NeedsHumanDecision()


def load_predicate(path: Path | None) -> PredicateFn:
    """Load a predicate function from a Python file.

    Returns the fail-safe (always NeedsHumanDecision) if path is None or loading fails.
    """
    if path is None:
        return _always_needs_human

    try:
        spec = importlib.util.spec_from_file_location("_approval_predicate", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load spec from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn: PredicateFn = module.decide
        logger.info("loaded predicate from %s", path)
        return fn
    except Exception:
        logger.exception("failed to load predicate from %s; defaulting to NeedsHumanDecision", path)
        return _always_needs_human


def call_predicate(fn: PredicateFn, server_namespace: str, tool_name: str, arguments: dict) -> PredicateDecision:
    """Call the predicate, catching exceptions and returning NeedsHumanDecision on error."""
    try:
        return fn(server_namespace, tool_name, arguments)
    except Exception:
        logger.exception(
            "predicate raised for server=%s tool=%s; defaulting to NeedsHumanDecision", server_namespace, tool_name
        )
        return NeedsHumanDecision()
