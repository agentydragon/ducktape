"""Improvement agent - re-exports from canonical locations.

The run_improvement_agent orchestration and result types have moved to AgentRegistry.
This module provides backwards-compatible re-exports.
"""

from __future__ import annotations

# Re-export result types from agent_registry (canonical location)
from props.orchestration.agent_registry import (
    ImprovementOutcome,
    ImprovementResult,
    OutcomeExhausted,
    OutcomeUnexpectedTermination,
)

__all__ = ["ImprovementOutcome", "ImprovementResult", "OutcomeExhausted", "OutcomeUnexpectedTermination"]
