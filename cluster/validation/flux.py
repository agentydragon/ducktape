"""Flux resource models and parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import durationpy
import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from cluster.cdk8s.manifest_roots import MANIFEST_ROOTS
from cluster.validation.k8s import Condition, SecretRef


class DependsOn(BaseModel):
    """Flux Kustomization dependency reference."""

    model_config = ConfigDict(extra="ignore")

    name: str
    namespace: str | None = None


class SourceRef(BaseModel):
    """Flux Kustomization spec.sourceRef — the GitRepository/OCIRepository/ExternalArtifact it reconciles from."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)

    kind: str = ""
    name: str = ""
    namespace: str | None = None


class HealthCheck(BaseModel):
    """Flux Kustomization health check reference."""

    model_config = ConfigDict(extra="ignore")

    kind: str = ""
    name: str = ""
    namespace: str = ""


class Decryption(BaseModel):
    """Flux Kustomization spec.decryption — declares how Flux decrypts SOPS
    Secrets under the path. A SOPS-encrypted Secret needs both `provider: sops`
    and a `secretRef.name` pointing at the age-key Secret: without the provider
    Flux applies ENC[...] ciphertext literally; without the secretRef it has no
    key to decrypt with. Both fail silently."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)

    provider: str = ""
    secret_ref: SecretRef | None = None


class FluxKustomizationSpec(BaseModel):
    """Parsed spec from a Flux Kustomization CR."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)

    path: str = ""
    # Namespace of this Kustomization CR (from metadata.namespace), populated by
    # parse_flux_kustomizations. Flux resolves a bare dependsOn/sourceRef entry
    # (no explicit namespace) in this namespace, so cross-namespace validation
    # needs it. Default "" means "unknown" — cross-namespace checks skip it.
    namespace: str = ""
    target_namespace: str | None = None
    manifest_path: Path | None = Field(default=None, exclude=True)
    depends_on: list[DependsOn] = []
    health_checks: list[HealthCheck] = []
    source_ref: SourceRef | None = None
    retry_interval: str | None = None
    wait: bool = False
    suspend: bool = False
    parked: bool = False
    decryption: Decryption | None = None

    def local_dir(self, repo_root: Path) -> Path | None:
        """Resolve spec.path to a directory under one of the manifest roots in `repo_root`, or
        None if it lies outside them (another repository, or a path read from source)."""
        rel = self.path.removeprefix("./")
        if not rel.startswith(tuple(f"{root}/" for root in MANIFEST_ROOTS)):
            return None
        return (repo_root / rel).resolve()


def parse_go_duration(s: str | None) -> float:
    """Convert a Flux Go duration to seconds; a missing duration is zero.

    durationpy uses timedelta, so the result has microsecond precision.
    """
    if s is None:
        return 0.0
    return durationpy.from_str(s).total_seconds()


class _HistoryMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)
    revision: str = ""


class FluxReconcileHistoryEntry(BaseModel):
    """One entry in `Kustomization.status.history` (Flux 2.7+). Each entry summarises
    reconcile attempts against a single `(revision, build digest)`:

    - `total_reconciliations` — count of attempts at this revision/digest.
    - `last_reconciled_status` — outcome of the most recent attempt
      (`ReconciliationSucceeded`, `HealthCheckFailed`, `ReconciliationFailed`, …).
    - `last_reconciled_duration` — Go-style duration of the most recent attempt
      (`parse_go_duration` available for converting to seconds).
    - `last_reconciled` — timestamp of the most recent attempt.

    Notes:

    - History is bounded by the controller's history size (default 10
      entries on v2.7+). Long windows can exceed retention.
    - The error message per failed attempt is not in history — fetch the
      current condition's message or a `Warning` Event for that.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    last_reconciled: datetime | None = None
    last_reconciled_duration: str | None = None
    last_reconciled_status: str = ""
    metadata: _HistoryMetadata = _HistoryMetadata()
    total_reconciliations: int = 0

    @property
    def revision(self) -> str:
        return self.metadata.revision

    @property
    def duration_s(self) -> float:
        return parse_go_duration(self.last_reconciled_duration)


class FluxStatus(BaseModel):
    """Current conditions reported by Flux controllers."""

    conditions: list[Condition] = []

    def condition(self, type_: str) -> Condition | None:
        """Return the first condition with the given type, or None."""
        return next((c for c in self.conditions if c.type == type_), None)

    @property
    def ready(self) -> Condition | None:
        """Convenience: the `Ready` condition, the one consumers want 99% of the time."""
        return self.condition("Ready")


class FluxKustomizationStatus(FluxStatus):
    """Live Kustomization status with its per-revision reconcile history."""

    history: list[FluxReconcileHistoryEntry] = []

    def history_in_window(self, since: datetime) -> list[FluxReconcileHistoryEntry]:
        """History entries whose `last_reconciled` falls inside the window
        starting at `since`. `since` should be tz-aware (UTC)."""
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        return [e for e in self.history if e.last_reconciled is not None and e.last_reconciled >= since]

    @property
    def latest_history(self) -> FluxReconcileHistoryEntry | None:
        """The history entry with the most recent `last_reconciled` timestamp."""
        with_ts = [e for e in self.history if e.last_reconciled is not None]
        if not with_ts:
            return None
        return max(with_ts, key=lambda e: e.last_reconciled)  # type: ignore[arg-type,return-value]


class _ObjectMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    namespace: str = ""
    annotations: dict[str, str] = Field(default_factory=dict)


class _FluxKustomizationDoc(BaseModel):
    """Top-level structure of a Flux Kustomization YAML document."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)

    api_version: str
    kind: str
    metadata: _ObjectMeta
    spec: FluxKustomizationSpec = FluxKustomizationSpec()


def parse_flux_kustomizations(flux_file: Path) -> dict[str, FluxKustomizationSpec]:
    """Parse a file of Flux Kustomization documents, returning {name: spec} for each."""
    results: dict[str, FluxKustomizationSpec] = {}
    with flux_file.open() as f:
        for doc in yaml.safe_load_all(f):
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") != "Kustomization":
                continue
            if not (doc.get("apiVersion") or "").startswith("kustomize.toolkit.fluxcd.io"):
                continue
            parsed = _FluxKustomizationDoc.model_validate(doc)
            results[parsed.metadata.name] = parsed.spec.model_copy(
                update={
                    "namespace": parsed.metadata.namespace,
                    "manifest_path": flux_file,
                    "parked": parsed.metadata.annotations.get("ducktape.org/parked") == "true",
                }
            )

    return results


# Flux source CR kinds that a Kustomization.spec.sourceRef can reference.
# Consumed by parse_cluster to build ParsedCluster.flux_sources — including the
# bootstrap GitRepository under flux-system/, which the app-manifest skip there
# would otherwise miss as a sourceRef resolution target. ExternalArtifact is not
# a Git-declared kind: source-watcher creates one per ArtifactGenerator artifact,
# so parse_cluster indexes the generators instead.
FLUX_SOURCE_KINDS = {"GitRepository", "OCIRepository", "HelmRepository", "HelmChart"}
EXTERNAL_ARTIFACT_KIND = "ExternalArtifact"
