"""Pydantic models for the Claude Code statusline stdin JSON schema.

Claude Code pipes a JSON object to the statusline command's stdin after each
assistant message. See https://code.claude.com/docs/en/statusline for the
full schema.
"""

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    id: str
    display_name: str | None = None


class Workspace(BaseModel):
    current_dir: str
    project_dir: str | None = None


class Cost(BaseModel):
    total_cost_usd: float = 0.0
    total_duration_ms: int = 0
    total_api_duration_ms: int = 0
    total_lines_added: int = 0
    total_lines_removed: int = 0


class CurrentUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ContextWindow(BaseModel):
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    context_window_size: int = 0
    used_percentage: float | None = None
    remaining_percentage: float | None = None
    current_usage: CurrentUsage | None = None


class OutputStyle(BaseModel):
    name: str


class Vim(BaseModel):
    mode: str


class Agent(BaseModel):
    name: str


class Worktree(BaseModel):
    name: str
    path: str
    branch: str | None = None
    original_cwd: str
    original_branch: str | None = None


class Input(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cwd: str = ""
    session_id: str = ""
    transcript_path: str | None = None
    model: Model | None = None
    workspace: Workspace | None = None
    version: str | None = None
    output_style: OutputStyle | None = None
    cost: Cost | None = None
    context_window: ContextWindow | None = None
    exceeds_200k_tokens: bool = False
    vim: Vim | None = None
    agent: Agent | None = None
    worktree: Worktree | None = None
