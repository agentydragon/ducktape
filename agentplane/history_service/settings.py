"""History Service deployment settings and configuration file name."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from agentplane.identity.auth.listener import Listener
from agentplane.net.peertls.config import ServiceSPIFFEID


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


class EventBlockProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_bytes: int = Field(default=65536, ge=1, le=8 << 20)
    target_entries: int = Field(default=32, ge=1, le=256)
    codec: Literal["zstd"] = "zstd"
    level: int = Field(default=3, ge=1, le=4)


class IngressSettings(BaseModel):
    event_block_writes: bool = Field(default=False, exclude_if=lambda v: not v)
    event_block_profile: EventBlockProfile | None = Field(default=None, exclude_if=lambda v: v is None)
    event_block_target_bytes_by_kind: dict[str, int] | None = Field(default=None, exclude_if=lambda v: v is None)

    model_config = ConfigDict(extra="forbid")
    writer_namespaces: frozenset[str] = Field(
        min_length=1,
        description="Namespaces in which live workload Identity credential records are resolved for journal writers.",
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
    compact_execution_evidence: bool = Field(
        default=False,
        exclude_if=lambda value: not value,
        description="Pack BoundaryRecord and ExecutionProgress events into blocks. Enable only after all "
        "History readers support these block kinds.",
    )

    @field_serializer("writer_namespaces")
    def sorted_writer_namespaces(self, value: frozenset[str]) -> list[str]:
        return sorted(value)


class Settings(BaseModel):
    """Complete non-secret runtime settings, authored in the mounted YAML file."""

    config_file_env: ClassVar[str] = "AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE"
    model_config = ConfigDict(extra="forbid")

    reader_service_callers: frozenset[ServiceSPIFFEID] = Field(
        default=frozenset(),
        exclude_if=lambda value: not value,
        description="Exact component SVIDs granted backend-wide History read access.",
    )
    service_delegation: ServiceDelegationSettings | None = Field(
        default=None,
        exclude_if=lambda value: not value,
        description="Independent exact service actors allowed to read History as a live effective Identity.",
    )
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

    @field_serializer("reader_service_callers")
    def sorted_reader_service_callers(self, value: frozenset[ServiceSPIFFEID]) -> list[ServiceSPIFFEID]:
        return sorted(value)
