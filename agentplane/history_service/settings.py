"""History Service deployment settings and configuration file name."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_serializer, model_validator

from agentplane.identity.auth.listener import KubernetesBearerAuth, Listener, SPIFFEAuth
from agentplane.subjects import ServiceAccountRef

RUNNER_INGRESS_AUDIENCE = "agentplane-runner-ingress"
type ServiceAccountName = Annotated[str, StringConstraints(pattern=r"^[^/]+/[^/]+$")]


class DelegationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: ServiceAccountName = Field(description="Actions ServiceAccount trusted to delegate, as namespace/name.")
    identity_callers: Literal[True] = Field(
        description="Must be true: Actions may assert a live Identity in policy_namespace as the effective caller."
    )
    methods: tuple[str, ...] = Field(
        min_length=1, description="Full HistoryService RPC method names that admit delegation."
    )


class IngressSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_audience: str = Field(min_length=1, description="The token audience for journal pushes by central samplers.")
    writer_namespaces: frozenset[str] = Field(
        min_length=1, description="Namespaces whose ServiceAccounts may push: the ones Sandboxes run in."
    )
    sampler_accounts: frozenset[ServiceAccountRef] = Field(
        default=frozenset(), description="ServiceAccounts allowed to publish centrally hosted Sessions."
    )
    sampler_identity_callers: bool = Field(
        default=False,
        description="Whether configured sampler ServiceAccounts may push as a delegating actor for a Session Identity.",
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

    reader_accounts: frozenset[ServiceAccountRef] = Field(min_length=1)
    delegation: DelegationSettings | None = Field(
        default=None,
        description="Independent Actions delegation trust; a delegated actor keeps ordinary caller read scope.",
    )
    token_audience: str = Field(min_length=1)
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

    @model_validator(mode="after")
    def bearer_audiences(self) -> Settings:
        audiences = frozenset(
            audience
            for method in self.rpc.auth
            if isinstance(method, KubernetesBearerAuth)
            for audience in method.audiences
        )
        if not {self.token_audience, self.ingress.token_audience} <= audiences:
            raise ValueError("rpc.auth must allow the read and ingress bearer audiences")
        return self

    @model_validator(mode="after")
    def sampler_identity_callers_need_ingress_identity_trust(self) -> Settings:
        if self.ingress.sampler_identity_callers and (
            not self.ingress.sampler_accounts or not any(isinstance(method, SPIFFEAuth) for method in self.rpc.auth)
        ):
            raise ValueError(
                "ingress.sampler_identity_callers requires ingress.sampler_accounts and a spiffe rpc listener"
            )
        return self
