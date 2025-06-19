"""Shared paths and constants for multi-agent teams."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

# Base directory for all team data
TEAMS_BASE = Path.home() / ".ai-teams"

# Message types
MESSAGE_TYPES = [
    "STATUS",
    "PROGRESS",
    "COMPLETE",
    "BLOCKER",
    "BLOCKER_RESOLVED",
    "HANDOFF",
    "HANDOFF_ACCEPTED",
    "DISCOVERY",
    "FYI",
    "CRITIQUE",
    "ABORT",
    "DIRECT",
]

MessageType = Literal[
    "STATUS",
    "PROGRESS",
    "COMPLETE",
    "BLOCKER",
    "BLOCKER_RESOLVED",
    "HANDOFF",
    "HANDOFF_ACCEPTED",
    "DISCOVERY",
    "FYI",
    "CRITIQUE",
    "ABORT",
    "DIRECT",
]


class ChannelMessage(BaseModel):
    """A message in the team communication channel."""

    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    agent: str
    type: MessageType
    message: str
    data: dict[str, Any] | None = None


@dataclass
class Team:
    """Represents a multi-agent team with all its paths."""

    team_id: str

    @property
    def base_dir(self) -> Path:
        """Base directory for this team."""
        return TEAMS_BASE / self.team_id

    @property
    def worktree_base(self) -> Path:
        """Base directory for all worktrees in this team."""
        return TEAMS_BASE / "worktrees" / self.team_id

    @property
    def channel_path(self) -> Path:
        """Path to the team's communication channel."""
        return self.base_dir / "channel.jsonl"

    @property
    def dashboard_path(self) -> Path:
        """Path to the team's dashboard."""
        return self.base_dir / "dashboard.json"

    @property
    def dirty_state_path(self) -> Path:
        """Path to saved dirty state SHA."""
        return self.base_dir / "dirty-state.sha"

    @property
    def team_branch_file(self) -> Path:
        """Path to file storing team branch name."""
        return self.base_dir / "team-branch.txt"

    @property
    def team_branch(self) -> str:
        """Git branch name for the team."""
        return f"ai-team/{self.team_id}/master"

    def agent_branch(self, agent_name: str) -> str:
        """Git branch name for an agent."""
        return f"ai-team/{self.team_id}/{agent_name}"

    def agent_worktree(self, agent_name: str) -> Path:
        """Worktree path for an agent."""
        return self.worktree_base / agent_name

    def agent_scratch_dir(self, agent_name: str) -> Path:
        """Scratch directory for an agent (relative to repo root)."""
        return Path("scratch") / self.team_id / agent_name

    def task_params_path(self, agent_name: str) -> Path:
        """Path to task parameters for an agent."""
        return self.base_dir / f"task-params-{agent_name}.json"

    def send_message(self, message: ChannelMessage) -> None:
        """Send a message to the team channel."""
        with self.channel_path.open("a") as f:
            f.write(message.model_dump_json() + "\n")
