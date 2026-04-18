"""Result types for the Grocy MCP eval."""

from __future__ import annotations

from pydantic import BaseModel


class EvalResult(BaseModel):
    case_id: str
    success_criteria: str
    model: str
    api: str
    postmortem_text: str
