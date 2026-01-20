"""Prompt optimizer agent tool argument and result models.

Local tools (exec, submit, report_failure) run in-container.
Remote tools (run_critic, run_grader) are accessed via MCP-over-HTTP from host.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# --- Local tool argument models ---


class SubmitArgs(BaseModel):
    """Arguments for submit tool."""

    summary: str = Field(..., description="Summary of the optimization results and findings")


class ReportFailureArgs(BaseModel):
    """Arguments for report_failure tool."""

    message: str = Field(..., description="Description of why optimization could not be completed")
