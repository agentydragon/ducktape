"""The console's non-secret deploy catalog (`haku.console.identity.config.ConsoleConfigFile`).

Secret leaves stay out of it: the Deployment overlays them from Secrets through
`HAKU_CONSOLE__*` environment variables (`console.py` names each one).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from haku.console.identity.config import ConsoleConfigFile


class _RetiredPolicy(BaseModel):
    id: str
    type: Literal["never"]


class RollingConsoleConfig(ConsoleConfigFile):
    # The projected file must remain readable by the previous API image during rollout.
    auto_approval_policies: list[_RetiredPolicy] = Field(default_factory=list)
    mcp: dict[str, dict[str, str]] = Field(default_factory=dict)


def config() -> dict[str, Any]:
    return {
        # Retain retired policy fields until every API replica runs the identity-only config
        # model; older images require them during the ConfigMap/image rolling update.
        "auto_approval_policies": [{"id": "manual_review", "type": "never"}],
        "access_profiles": [{"id": "manual-review", "auto_approval_policy": "manual_review"}],
        "default_access_profile_id": "manual-review",
        "mcp": {"servers": {}},
    }
