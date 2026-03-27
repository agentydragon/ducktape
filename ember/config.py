from __future__ import annotations

import os
import tomllib

# TODO: Consider using pydantic-settings for cleaner env var + file config loading
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from openai.types.responses import ResponseIncludable
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ember.secrets import ProjectedSecret
from ember.system_prompt import load_system_prompt
from openai_utils.types import ReasoningEffort


class _SleepPolicyBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LegacySleepUntilUserMessagePolicy(_SleepPolicyBase):
    kind: Literal["legacy"] = "legacy"


class EnforcedSleepUntilUserMessagePolicy(_SleepPolicyBase):
    kind: Literal["enforced"] = "enforced"
    timeout_seconds: int = 30

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("timeout_seconds must be positive")
        return value

    @property
    def timeout(self) -> timedelta:
        return timedelta(seconds=self.timeout_seconds)


SleepUntilUserMessagePolicy = Annotated[
    LegacySleepUntilUserMessagePolicy | EnforcedSleepUntilUserMessagePolicy, Field(discriminator="kind")
]


def _parse_sleep_policy(cfg: dict[str, Any]) -> SleepUntilUserMessagePolicy:
    """Parse sleep policy config dict into typed model."""
    if not cfg:
        return LegacySleepUntilUserMessagePolicy()
    if cfg.get("kind") == "enforced":
        return EnforcedSleepUntilUserMessagePolicy.model_validate(cfg)
    return LegacySleepUntilUserMessagePolicy.model_validate(cfg)


class MatrixSettings(BaseModel):
    """Matrix configuration for the pilot."""

    base_url: str | None
    access_token_secret: ProjectedSecret
    admin_user_id: str | None = None
    state_store: Path
    store_dir: Path
    device_id: str = "ember-device"
    # TODO(k3s/ember): Source pickle_key from a k8s secret instead of TOML to
    # keep Megolm session dumps encrypted at rest in prod.
    pickle_key: str = "ember-matrix-store"
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.access_token_secret.value())


class OpenAISettings(BaseModel):
    api_key_secret: ProjectedSecret
    model: str
    system_prompt: str
    sleep_tool_policy: SleepUntilUserMessagePolicy = LegacySleepUntilUserMessagePolicy()
    api_base: str | None = None
    reasoning_effort: ReasoningEffort = ReasoningEffort.MEDIUM
    include_encrypted_reasoning: bool = True
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    @property
    def include(self) -> list[ResponseIncludable]:
        includes: list[ResponseIncludable] = []
        if self.include_encrypted_reasoning:
            includes.append(cast(ResponseIncludable, "reasoning.encrypted_content"))
        return includes


class EmberSettings(BaseModel):
    matrix: MatrixSettings
    openai: OpenAISettings
    history_path: Path
    state_dir: Path
    workspace_path: Path
    model_config = ConfigDict(frozen=True, extra="forbid")


def load_settings() -> EmberSettings:
    """Load Ember settings from TOML configuration and mounted secrets."""

    config_path = Path(os.getenv("EMBER_CONFIG_FILE", "/etc/ember/ember.toml")).expanduser()
    config_data: dict[str, Any] = {}
    if config_path.exists():
        try:
            config_data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover
            raise RuntimeError(f"Invalid Ember config file: {exc}") from exc

    matrix_cfg = config_data.get("matrix", {}) if isinstance(config_data.get("matrix"), dict) else {}
    state_cfg = config_data.get("state", {}) if isinstance(config_data.get("state"), dict) else {}
    openai_cfg = config_data.get("openai", {}) if isinstance(config_data.get("openai"), dict) else {}
    sleep_tool_cfg = openai_cfg.get("sleep_tool", {}) if isinstance(openai_cfg.get("sleep_tool"), dict) else {}

    if "kind" not in sleep_tool_cfg and "mode" in sleep_tool_cfg:
        sleep_tool_cfg = dict(sleep_tool_cfg)
        sleep_tool_cfg["kind"] = sleep_tool_cfg.pop("mode")

    state_dir = Path(os.getenv("EMBER_STATE_DIR") or state_cfg.get("dir", "/var/lib/ember")).expanduser()
    workspace_dir = os.getenv("EMBER_WORKSPACE_DIR") or state_cfg.get("workspace_dir")
    workspace_path = (Path(workspace_dir) if workspace_dir else state_dir / "workspace").expanduser()
    history_path = state_dir / "pilot_history.jsonl"

    matrix_access_token = ProjectedSecret(name="matrix_access_token", env_var="MATRIX_ACCESS_TOKEN")
    openai_api_key = ProjectedSecret(name="openai_api_key", env_var="OPENAI_API_KEY")

    api_base = openai_cfg.get("api_base")
    if api_base and "OPENAI_API_BASE" not in os.environ:
        os.environ["OPENAI_API_BASE"] = str(api_base)

    try:
        return EmberSettings(
            matrix=MatrixSettings(
                base_url=os.getenv("MATRIX_BASE_URL") or matrix_cfg.get("base_url"),
                access_token_secret=matrix_access_token,
                admin_user_id=os.getenv("MATRIX_ADMIN_USER_ID") or matrix_cfg.get("admin_user_id"),
                state_store=state_dir / "matrix_state.json",
                store_dir=state_dir / "matrix_store",
                device_id=matrix_cfg.get("device_id", "ember-device"),
                pickle_key=matrix_cfg.get("pickle_key", "ember-matrix-store"),
            ),
            openai=OpenAISettings(
                api_key_secret=openai_api_key,
                model=os.getenv("OPENAI_MODEL") or openai_cfg.get("model", "gpt-5-codex"),
                system_prompt=load_system_prompt(),
                api_base=openai_cfg.get("api_base"),
                reasoning_effort=cast(
                    ReasoningEffort,
                    os.getenv("OPENAI_REASONING_EFFORT") or openai_cfg.get("reasoning_effort", "medium"),
                ),
                include_encrypted_reasoning=_env_flag(
                    "OPENAI_INCLUDE_ENCRYPTED_REASONING",
                    default=bool(openai_cfg.get("include_encrypted_reasoning", True)),
                ),
                sleep_tool_policy=_parse_sleep_policy(sleep_tool_cfg),
            ),
            history_path=history_path,
            state_dir=state_dir,
            workspace_path=workspace_path,
        )
    except ValidationError as exc:  # pragma: no cover - configuration errors should surface loudly
        raise RuntimeError(f"Invalid pilot configuration: {exc}") from exc


def _env_flag(name: str, default: bool) -> bool:
    if (raw := os.getenv(name)) is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default
