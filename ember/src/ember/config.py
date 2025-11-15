from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from openai.types.responses import ResponseIncludable
from openai.types.shared.reasoning_effort import ReasoningEffort
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator
import tomllib

from .secrets import ProjectedSecret
from .system_prompt import load_system_prompt


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
_SLEEP_POLICY_ADAPTER = TypeAdapter(SleepUntilUserMessagePolicy)


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


class ObjectStoreSettings(BaseModel):
    endpoint: str
    bucket: str
    access_key_secret: ProjectedSecret
    secret_key_secret: ProjectedSecret
    secure: bool = True
    url_expiry_seconds: int = Field(default=120, ge=1)
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)


class OpenAISettings(BaseModel):
    api_key_secret: ProjectedSecret
    model: str
    system_prompt: str
    sleep_tool_policy: SleepUntilUserMessagePolicy = LegacySleepUntilUserMessagePolicy()
    api_base: str | None = None
    reasoning_effort: ReasoningEffort = "medium"
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
    object_store: ObjectStoreSettings | None = None
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

    object_store_cfg = config_data.get("object_store", {}) if isinstance(config_data.get("object_store"), dict) else {}

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

    sleep_policy = (
        _SLEEP_POLICY_ADAPTER.validate_python(sleep_tool_cfg) if sleep_tool_cfg else LegacySleepUntilUserMessagePolicy()
    )

    api_base = openai_cfg.get("api_base")
    if api_base and "OPENAI_API_BASE" not in os.environ:
        os.environ["OPENAI_API_BASE"] = str(api_base)

    object_store_settings: ObjectStoreSettings | None = None
    object_store_env_endpoint = os.getenv("OBJECT_STORE_ENDPOINT")
    object_store_env_bucket = os.getenv("OBJECT_STORE_BUCKET")
    if object_store_cfg or object_store_env_endpoint or object_store_env_bucket:
        endpoint = object_store_env_endpoint or object_store_cfg.get("endpoint")
        bucket = object_store_env_bucket or object_store_cfg.get("bucket")
        if not endpoint or not bucket:
            raise RuntimeError("Object store configuration missing endpoint or bucket")

        access_secret_name = object_store_cfg.get("access_key_secret", "object_store_access_key")
        secret_secret_name = object_store_cfg.get("secret_key_secret", "object_store_secret_key")
        object_store_settings = ObjectStoreSettings(
            endpoint=endpoint,
            bucket=bucket,
            access_key_secret=ProjectedSecret(
                name=access_secret_name, env_var=object_store_cfg.get("access_key_env", "OBJECT_STORE_ACCESS_KEY")
            ),
            secret_key_secret=ProjectedSecret(
                name=secret_secret_name, env_var=object_store_cfg.get("secret_key_env", "OBJECT_STORE_SECRET_KEY")
            ),
            secure=bool(object_store_cfg.get("secure", True)),
            url_expiry_seconds=int(object_store_cfg.get("url_expiry_seconds", 120)),
        )

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
                sleep_tool_policy=sleep_policy,
            ),
            history_path=history_path,
            state_dir=state_dir,
            workspace_path=workspace_path,
            object_store=object_store_settings,
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
