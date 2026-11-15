"""History Service deployment settings and configuration file name."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from agentplane.identity.auth.listener import Listener, SPIFFEAuth
from agentplane.net.peertls.config import ServiceSPIFFEID
from agentplane.subjects import ServiceAccountRef

RUNNER_INGRESS_AUDIENCE = "agentplane-runner-ingress"


class DelegationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str | None = None
    identity_callers: bool = False
    methods: tuple[str, ...] = ()


class ServiceDelegator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    spiffe_id: ServiceSPIFFEID
    connection_provenance: Literal[False] = False


class ServiceDelegationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    actors: tuple[ServiceDelegator, ...] = Field(min_length=1)
    methods: tuple[str, ...] = Field(
        min_length=1, description="Exact HistoryService RPC method names that admit service delegation."
    )


class IngressSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # CLEANUP(added 2026-11-15): Remove after manifests omit ingress.token_audience and that rollout completes.
    token_audience: str = Field(default="", description="Ignored legacy bearer audience.")
    writer_namespaces: frozenset[str] = Field(
        min_length=1,
        description="Namespaces in which live workload Identity credential records are resolved for journal writers.",
    )
    # CLEANUP(added 2026-11-15): Remove these ignored legacy sampler settings after manifests omit them and that rollout completes.
    sampler_accounts: frozenset[ServiceAccountRef] = Field(
        default_factory=frozenset, description="Ignored legacy ServiceAccount sampler allowlist."
    )
    sampler_identity_callers: bool = Field(
        default=False, description="Ignored legacy sampler Identity delegation setting."
    )
    sampler_service_actor: ServiceSPIFFEID | None = Field(
        default=None,
        exclude_if=lambda value: not value,
        description="Exact Central Sampler service SVID admitted to push, and to delegate Session Identity provenance.",
    )
    settle_deltas: bool = Field(
        default=True,
        description="Remove an item's streamed delta entries from stored history once its retained "
        "completion provably holds their concatenation. Turn off to keep every entry for debugging.",
    )
    compact_deltas: bool = Field(
        default=False,
        description="Pack native frames and streamed deltas into bounded compressed blocks before completion. "
        "Preserves all original events and cursors. Disabling affects new writes only.",
    )

    @field_serializer("writer_namespaces")
    def sorted_writer_namespaces(self, value: frozenset[str]) -> list[str]:
        return sorted(value)

    @field_serializer("sampler_accounts")
    def sorted_sampler_accounts(self, value: frozenset[ServiceAccountRef]) -> list[ServiceAccountRef]:
        return sorted(value, key=lambda account: (account.namespace, account.name))


class Settings(BaseModel):
    """Complete non-secret runtime settings, authored in the mounted YAML file."""

    config_file_env: ClassVar[str] = "AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE"
    model_config = ConfigDict(extra="forbid")

    # CLEANUP(added 2026-11-15): Remove these ignored legacy reader and account-delegation settings after manifests omit them and that rollout completes.
    reader_accounts: frozenset[ServiceAccountRef] = Field(
        default_factory=frozenset, description="Ignored legacy ServiceAccount reader allowlist."
    )
    reader_service_callers: frozenset[ServiceSPIFFEID] = Field(
        default=frozenset(),
        exclude_if=lambda value: not value,
        description="Exact component SVIDs granted backend-wide History read access.",
    )
    delegation: DelegationSettings | None = Field(default=None, description="Ignored legacy ServiceAccount delegation.")
    service_delegation: ServiceDelegationSettings | None = Field(
        default=None,
        exclude_if=lambda value: not value,
        description="Independent exact service actors allowed to read History as a live effective Identity.",
    )
    # CLEANUP(added 2026-11-15): Remove after manifests omit token_audience and that rollout completes.
    token_audience: str = Field(default="", description="Ignored legacy bearer audience.")
    request_timeout_s: float = Field(default=15, gt=0, le=60)
    migration_timeout_s: float = Field(default=600, ge=0.001, le=86400)
    rpc: Listener
    kubeconfig: Path | None = None
    policy_namespace: str = Field(min_length=1, description="Namespace holding the Identities a Sandbox SVID may name.")
    ingress: IngressSettings
    fold_concurrency: int = Field(default=4, ge=1, le=64)
    # A backfill advances each Session one page per cycle; the history store serves at most 1000.
    fold_page_size: int = Field(default=1000, ge=1, le=1000)
    fold_interval_s: float = Field(default=2, gt=0)
    native_frame_read_batch_size: int = Field(
        default=200, ge=1, description="Maximum linked sequences per raw-history query, independent of API pagination."
    )
    watch_lease_s: float = Field(
        default=300, gt=0, description="Seconds a WatchSessionSummaries stream lasts before planned renewal."
    )

    @field_serializer("reader_accounts")
    def sorted_reader_accounts(self, value: frozenset[ServiceAccountRef]) -> list[ServiceAccountRef]:
        return sorted(value, key=lambda account: (account.namespace, account.name))

    @field_serializer("reader_service_callers")
    def sorted_reader_service_callers(self, value: frozenset[ServiceSPIFFEID]) -> list[ServiceSPIFFEID]:
        return sorted(value)

    @model_validator(mode="after")
    def workload_svid_trust(self) -> Settings:
        if not any(isinstance(method, SPIFFEAuth) for method in self.rpc.auth):
            raise ValueError("rpc.auth must enable spiffe client trust")
        return self
