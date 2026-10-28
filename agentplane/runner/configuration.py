"""Runner's versioned file contract, shared by Pod and VM provisioning."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

CONFIG_DIRECTORY = "/run/agentplane/config"
CONFIG_FILE = f"{CONFIG_DIRECTORY}/runner.json"
LAUNCH_FILE = "/run/agentplane/sandbox/launch.json"
CA_BUNDLE = Path("/run/agentplane/trust/ca-certificates.crt")
JAVA_TRUST_STORE = "/run/agentplane/trust/ca-certificates.p12"
KUBECONFIG = f"{CONFIG_DIRECTORY}/kubeconfig"


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    target: str = Field(min_length=1, description="Service host:port.")
    bearer: str = Field(min_length=1, description="Bearer presented to the platform route.")


class ResourceLimits(BaseModel):
    """A cgroup-v2 budget; CPU quota is per fixed 100,000 microsecond period."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    cpu_quota_micros: int = Field(gt=0)
    memory_max_bytes: int = Field(gt=0)
    pids_max: int = Field(gt=0)
    read_bps: int = Field(gt=0)
    write_bps: int = Field(gt=0)
    read_iops: int = Field(gt=0)
    write_iops: int = Field(gt=0)


class GuestResources(BaseModel):
    """VM size, systemd service caps, and the delegated Runner cgroup budgets."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    guest_memory_bytes: int = Field(gt=0)
    guest_vcpu_count: int = Field(gt=0)
    os_reserve_bytes: int = Field(ge=0)
    service_memory_high_bytes: int = Field(gt=0)
    service_memory_max_bytes: int = Field(gt=0)
    service_cpu_quota_micros: int = Field(gt=0)
    service_pids_max: int = Field(gt=0)
    aggregate: ResourceLimits
    process: ResourceLimits
    executor: ResourceLimits | None = None

    @model_validator(mode="after")
    def internally_consistent(self) -> GuestResources:
        if self.os_reserve_bytes >= self.guest_memory_bytes:
            raise ValueError("os_reserve_bytes must be smaller than guest_memory_bytes")
        if self.service_memory_high_bytes > self.service_memory_max_bytes:
            raise ValueError("service_memory_high_bytes must not exceed service_memory_max_bytes")
        if self.service_memory_max_bytes > self.guest_memory_bytes - self.os_reserve_bytes:
            raise ValueError("service_memory_max_bytes must leave the configured OS reserve")
        if self.aggregate.memory_max_bytes > self.service_memory_high_bytes:
            raise ValueError("aggregate memory max must not exceed service memory high")
        if self.aggregate.cpu_quota_micros > self.service_cpu_quota_micros:
            raise ValueError("aggregate CPU quota must not exceed service CPU quota")
        if self.aggregate.pids_max > self.service_pids_max:
            raise ValueError("aggregate PID max must not exceed service PID max")
        if self.executor is not None:
            if self.executor.memory_max_bytes > self.service_memory_high_bytes - self.aggregate.memory_max_bytes:
                raise ValueError("harness and executor memory caps must fit below service memory high")
            if self.executor.pids_max >= self.service_pids_max - self.aggregate.pids_max:
                raise ValueError("harness and executor PID caps must leave room for Runner and supervisors")
            if self.executor.cpu_quota_micros > self.service_cpu_quota_micros:
                raise ValueError("executor CPU quota must fit within Runner service quota")
        if self.process.memory_max_bytes > self.aggregate.memory_max_bytes:
            raise ValueError("process memory max must not exceed aggregate memory max")
        if self.process.cpu_quota_micros > self.aggregate.cpu_quota_micros:
            raise ValueError("process CPU quota must not exceed aggregate CPU quota")
        if self.process.pids_max > self.aggregate.pids_max:
            raise ValueError("process PID max must not exceed aggregate PID max")
        if self.service_cpu_quota_micros > self.guest_vcpu_count * 100_000:
            raise ValueError("service CPU quota must not exceed guest vCPU capacity")
        if self.service_cpu_quota_micros % 1_000 != 0:
            raise ValueError("service CPU quota must convert to an integral systemd CPUQuota percent")
        return self


class EnvironmentVariables(BaseModel):
    """Inherited names are read first; explicit values take precedence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    inherit: Sequence[str] = Field(default_factory=list)
    values: Mapping[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_names(self) -> EnvironmentVariables:
        if any(not name or "=" in name or "\0" in name for name in (*self.inherit, *self.values)):
            raise ValueError("environment names must be nonempty and contain neither '=' nor NUL")
        if any("\0" in value for value in self.values.values()):
            raise ValueError("environment values must not contain NUL")
        return self


class Harness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    binary: str
    base_url: str
    token_env: str = Field(min_length=1, description="Runner environment variable containing the provider token.")

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        url = AnyHttpUrl(value)
        if url.username is not None or url.password is not None or url.query or url.fragment or url.port == 0:
            raise ValueError("provider URL must not contain credentials, query, fragment, or port zero")
        return value.rstrip("/")

    @field_validator("binary")
    @classmethod
    def absolute_binary(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("harness executable must be absolute")
        return value


class Executor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    url: str

    @field_validator("url")
    @classmethod
    def loopback_endpoint(cls, value: str) -> str:
        endpoint = urlsplit(value)
        if (
            endpoint.scheme != "ws"
            or endpoint.hostname != "127.0.0.1"
            or not endpoint.port
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path
            or endpoint.query
            or endpoint.fragment
            or "?" in value
            or "#" in value
        ):
            raise ValueError("executor URL must be ws://127.0.0.1:<nonzero port>")
        return value


class ContainerExecutor(Executor):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    socket: str

    @field_validator("socket")
    @classmethod
    def absolute_socket(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("container executor socket must be absolute")
        return value


class CgroupExecutor(Executor):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    root: str


class PodEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["pod"] = "pod"
    executor: ContainerExecutor | None = None


class VMEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["vm"] = "vm"
    resources: GuestResources
    console_autologin: bool = False
    root: str
    agent_uid: int = Field(ge=1, le=4_294_967_295)
    agent_gid: int = Field(ge=1, le=4_294_967_295)
    block_devices: Sequence[str] = Field(min_length=1)
    executor: CgroupExecutor | None = None

    @model_validator(mode="after")
    def valid_isolation(self) -> VMEnvironment:
        if not Path(self.root).is_absolute() or any(not Path(path).is_absolute() for path in self.block_devices):
            raise ValueError("cgroup and block-device paths must be absolute")
        if (self.executor is None) != (self.resources.executor is None):
            raise ValueError("executor endpoint and resource budget must be supplied together")
        if self.executor is not None:
            root, executor_root = Path(self.root), Path(self.executor.root)
            if not executor_root.is_absolute() or root == executor_root or root.parent != executor_root.parent:
                raise ValueError("executor and harness cgroups must be distinct absolute siblings")
        return self


class DebugCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    command_id: str = Field(min_length=1)


class Configuration(BaseModel):
    """All Runner policy supplied by its template, separate from Sandbox launch identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[2]
    session_reconcile_concurrency: int | None = Field(
        default=None,
        gt=0,
        description="Optional maximum concurrent Session COSI reconciliations; does not limit Session admission.",
    )
    state_dir: str
    workspace_root: str | None = None
    initialization_cwd: str
    control: Endpoint
    ingress: Endpoint
    platform_instructions: str = Field(min_length=1)
    runner_environment: EnvironmentVariables = Field(default_factory=EnvironmentVariables)
    harness_environment: EnvironmentVariables
    claude: Harness | None = None
    codex: Harness | None = None
    environment: Annotated[PodEnvironment | VMEnvironment, Field(discriminator="kind")]
    debug_checkpoint: DebugCheckpoint | None = None

    @model_validator(mode="after")
    def valid_configuration(self) -> Configuration:
        if any(not Path(path).is_absolute() for path in (self.state_dir, self.initialization_cwd)):
            raise ValueError("state and initialization paths must be absolute")
        if self.workspace_root is not None and not Path(self.workspace_root).is_absolute():
            raise ValueError("workspace_root must be absolute when configured")
        if self.environment.executor is not None and self.codex is None:
            raise ValueError("executor requires Codex")
        return self
