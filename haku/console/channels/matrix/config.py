"""The Matrix adapter's deploy configuration: its own wiring, and its slice of the shared file.

Two halves, one binary. `Config` is the channel's own env-driven wiring (homeserver, identities,
the bot credential). `AdapterConfigFile` is the worker's narrow read of the deploy-owned console
config file — the launch-identity registry and explicit Matrix room-creation route a room bind
consults. The console-only siblings
(MCP catalog, policies, recall indexes) are deliberately unmodeled and unvalidated here: the worker
must start without them and must not fail when their vocabularies move ahead of this image (one
binary, one config — <../../docs/naming_and_layout.md> §5).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from haku.console.harnesses.kind import HarnessKind


class Config(BaseModel):
    """Wiring for the Matrix channel (<SPEC.md>)."""

    model_config = ConfigDict(frozen=True)

    homeserver: str
    user_id: str
    operator_user_id: str = Field(description="The only MXID whose room invitations are joined.")
    operator_subject: str = Field(
        description=(
            "The Authentik `sub_mode=user_id` value for `operator_user_id`, resolved once to a "
            "canonical Operator UUID. Matrix has no OIDC identity of its own, so this deploy-time "
            "pair is the whole sender-to-Operator mapping; the MXID never carries authority "
            "on its own."
        )
    )
    device_id: str = Field(
        # The literal predates the adapter worker: renaming it would register a new device with the
        # homeserver and orphan the one every cached token belongs to.
        default="haku-console",
        description="Pinned so repeated logins reuse one device instead of leaving a new one per restart.",
    )
    password: SecretStr = Field(
        description=(
            "The bot credential the sync loop logs in with. Required: running the loop is this "
            "worker's whole job, so a missing credential fails startup loudly rather than idling."
        )
    )


class ConfiguredAgent(BaseModel):
    """The static-agent slice: which access profile each launchable identity carries."""

    agent_id: UUID
    access_profile_id: str


class ConfiguredProfile(BaseModel):
    """The access-profile slice: which harnesses a profile admits."""

    id: str
    allowed_harnesses: set[HarnessKind] = Field(default_factory=set)


class LaunchableEntry(BaseModel):
    """The launchable-agent slice: membership only; prompts stay the console's."""

    agent_id: UUID


class HarnessEntry(BaseModel):
    """One configured harness's slice: the Agent identity it registers."""

    agent_id: UUID


class Harnesses(BaseModel):
    claude_code: HarnessEntry | None = None
    codex_app_server: HarnessEntry | None = None


class MatrixLaunchConfig(BaseModel):
    """The Matrix-only launch route for rooms that do not carry a harness selector."""

    default_agent_id: UUID = Field(
        description="Agent identity to stamp on a new Matrix conversation when the room has no selector."
    )
    default_harness_kind: HarnessKind = Field(
        description="Harness kind to stamp on a new Matrix conversation when the room has no selector."
    )


class AdapterConfigFile(BaseModel):
    harnesses: Harnesses | None = None
    access_profiles: tuple[ConfiguredProfile, ...] = ()
    static_agents: tuple[ConfiguredAgent, ...] = ()
    launchable_agents: tuple[LaunchableEntry, ...] = ()
    matrix: MatrixLaunchConfig | None = Field(
        default=None,
        description=(
            "Explicit Agent and harness route for a new Matrix conversation: invites and messages "
            "carry no selector. Existing rooms follow their conversation row."
        ),
    )


def load_adapter_config(path: Path) -> AdapterConfigFile:
    if not path.is_file():
        raise RuntimeError(f"haku-matrix-adapter config file does not exist: {path}")
    return AdapterConfigFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
