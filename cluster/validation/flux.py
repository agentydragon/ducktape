"""Flux domain: models, parsing, and build execution."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from cluster.validation.k8s import Condition, parse_k8s_resources
from cluster.validation.tool_resolve import resolve_tool


class DependsOn(BaseModel):
    """Flux Kustomization dependency reference."""

    model_config = ConfigDict(extra="ignore")

    name: str
    namespace: str | None = None


class HealthCheck(BaseModel):
    """Flux Kustomization health check reference."""

    model_config = ConfigDict(extra="ignore")

    kind: str = ""
    name: str = ""
    namespace: str = ""


class Decryption(BaseModel):
    """Flux Kustomization spec.decryption — declares how Flux decrypts SOPS
    Secrets under the path. `provider: sops` is required for any SOPS-encrypted
    Secret to apply as plaintext instead of literal ENC[...] ciphertext."""

    model_config = ConfigDict(extra="ignore")

    provider: str = ""


class FluxKustomizationSpec(BaseModel):
    """Parsed spec from a Flux Kustomization CR."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)

    path: str = ""
    depends_on: list[DependsOn] = []
    health_checks: list[HealthCheck] = []
    retry_interval: str | None = None
    wait: bool = False
    suspend: bool = False
    decryption: Decryption | None = None

    def local_dir(self, k8s_dir: Path, k8s_subpath: str = "cluster/k8s") -> Path | None:
        """Resolve spec.path to a local directory under k8s_dir, or None if external."""
        rel = self.path.removeprefix("./")
        prefix = k8s_subpath + "/"
        if not rel.startswith(prefix):
            return None
        return (k8s_dir / rel[len(prefix) :]).resolve()


class InventoryEntry(BaseModel):
    """One applied resource in a Kustomization's status.inventory.

    `id` format is `<namespace>_<name>_<group>_<kind>` (empty namespace for
    cluster-scoped resources). `v` is the apiVersion."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    id: str
    v: str = ""


class Inventory(BaseModel):
    """Flux Kustomization.status.inventory wrapper."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    entries: list[InventoryEntry] = []


# Go time.Duration string components (1h30m45.5s, 940.5ms, 7µs, etc.).
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_DURATION_UNIT_S = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_go_duration(s: str | None) -> float:
    """Parse a Go time.Duration string ("7.9s", "10m1.219s", "940.5ms") to
    seconds. Returns 0.0 for empty / unparseable input."""
    if not s:
        return 0.0
    total = 0.0
    matched = False
    for value, unit in _DURATION_RE.findall(s):
        matched = True
        total += float(value) * _DURATION_UNIT_S[unit]
    return total if matched else 0.0


class _HistoryMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)
    revision: str = ""


class FluxReconcileHistoryEntry(BaseModel):
    """One per-revision entry in `Kustomization.status.history` / `HelmRelease.status.history`
    (Flux 2.7+). Each entry summarises every reconcile attempt against a single
    `(revision, build digest)`:

    - `total_reconciliations` — count of attempts at this revision/digest.
    - `last_reconciled_status` — outcome of the most recent attempt
      (`ReconciliationSucceeded`, `HealthCheckFailed`, `ReconciliationFailed`, …).
    - `last_reconciled_duration` — Go-style duration of the most recent attempt
      (`parse_go_duration` available for converting to seconds).
    - `first_reconciled` / `last_reconciled` — first/last attempt timestamps.

    Notes:

    - History is bounded by the controller's history size (default 10
      entries on v2.7+). Long windows can exceed retention.
    - The error message per failed attempt is not in history — fetch the
      current condition's message or a `Warning` Event for that.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    digest: str = ""
    first_reconciled: datetime | None = None
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


class FluxKustomizationStatus(BaseModel):
    """Parsed status from a live Flux Kustomization CR (live API responses only;
    YAML on disk doesn't carry status). Same shape works for HelmRelease too —
    the conditions / history / lastAppliedRevision / lastAttemptedRevision
    fields are identical."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)

    conditions: list[Condition] = []
    history: list[FluxReconcileHistoryEntry] = []
    inventory: Inventory | None = None
    last_applied_revision: str | None = None
    last_attempted_revision: str | None = None
    last_handled_reconcile_at: str | None = None
    observed_generation: int | None = None

    def condition(self, type_: str) -> Condition | None:
        """Return the first condition with the given type, or None."""
        return next((c for c in self.conditions if c.type == type_), None)

    @property
    def ready(self) -> Condition | None:
        """Convenience: the `Ready` condition, the one consumers want 99% of the time."""
        return self.condition("Ready")

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


class _FluxKustomizationDoc(BaseModel):
    """Top-level structure of a Flux Kustomization YAML document."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)

    api_version: str
    kind: str
    metadata: _ObjectMeta
    spec: FluxKustomizationSpec = FluxKustomizationSpec()


def parse_flux_kustomizations(flux_file: Path) -> dict[str, FluxKustomizationSpec]:
    """Parse a flux-kustomization.yaml file, returning {name: spec} for each document."""
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
            results[parsed.metadata.name] = parsed.spec

    return results


async def run_flux_build(k8s_dir: Path) -> tuple[int, str, str]:
    """Run flux build and return (returncode, stdout, stderr)."""
    kustomization_file = k8s_dir / "flux-system" / "gotk-sync.yaml"

    if not kustomization_file.exists():
        raise FileNotFoundError(f"gotk-sync.yaml not found at {kustomization_file}")

    flux_bin = resolve_tool("flux", "multitool/tools/flux/flux")
    proc = await asyncio.create_subprocess_exec(
        flux_bin,
        "build",
        "kustomization",
        "flux-system",
        "--path",
        k8s_dir,
        "--kustomization-file",
        kustomization_file,
        "--dry-run",
        "--verbose",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=60)
    return await proc.wait(), stdout_bytes.decode(), stderr_bytes.decode()


async def validate_flux_build(k8s_dir: Path) -> list[str]:
    """Validate flux build."""
    try:
        returncode, stdout, stderr = await run_flux_build(k8s_dir)
    except FileNotFoundError as e:
        return [str(e)]

    if returncode != 0:
        return [f"flux build failed:\nk8s_dir: {k8s_dir}\nstdout:\n{stdout}\nstderr:\n{stderr}"]

    if not stdout.strip():
        return [f"flux build returned empty output:\nk8s_dir: {k8s_dir}\nstderr: {stderr.strip() or 'none'}"]

    errors = []
    resource_counts: Counter[str] = Counter()

    for resource in parse_k8s_resources(yaml.safe_load_all(stdout)):
        resource_counts[resource.kind] += 1

    if resource_counts.get("Kustomization", 0) == 0:
        errors.append("No Flux Kustomization resources found in flux build output")
    if resource_counts.get("GitRepository", 0) == 0:
        errors.append("No GitRepository resource found in flux build output")

    return errors
