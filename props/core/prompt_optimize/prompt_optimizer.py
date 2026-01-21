"""Prompt optimizer - re-exports from canonical locations.

The PromptEvalServer and related types have moved to props.core.prompt_eval_server.
The run_prompt_optimizer orchestration has moved to AgentRegistry.run_prompt_optimizer.

This module provides backwards-compatible re-exports.
"""

from __future__ import annotations

# Re-export from prompt_eval_server (canonical location)
from props.core.prompt_eval_server import (
    PromptEvalServer,
    PromptOptimizerState,
    ReportFailureInput,
    RunCriticInput,
    RunCriticOutput,
    WaitUntilGradedInput,
    WaitUntilGradedOutput,
)

# Re-export target metric
from props.core.prompt_optimize.target_metric import TargetMetric

__all__ = [
    "PromptEvalServer",
    "PromptOptimizerState",
    "ReportFailureInput",
    "RunCriticInput",
    "RunCriticOutput",
    "TargetMetric",
    "WaitUntilGradedInput",
    "WaitUntilGradedOutput",
]
