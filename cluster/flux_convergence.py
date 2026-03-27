"""Flux kustomization convergence monitoring.

Models, phase derivation, and polling loop for watching Flux kustomizations
converge to Ready state during cluster bootstrap.
"""

import logging
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from kubernetes import client
from kubernetes.client import ApiException
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class KustomizationPhase(StrEnum):
    PENDING = "Pending"
    RECONCILING = "Reconciling"
    DEP_WAIT = "DepWait"
    FAILED = "Failed"
    STALLED = "Stalled"
    READY = "Ready"


class FluxCondition(BaseModel):
    """Mirrors metav1.Condition from the Flux kustomize-controller API."""

    type: str
    status: str
    reason: str = ""
    message: str = ""


class ObjectMeta(BaseModel):
    name: str


class KustomizationStatus(BaseModel):
    conditions: list[FluxCondition] = []


class FluxKustomization(BaseModel):
    """Partial model of kustomize.toolkit.fluxcd.io/v1 Kustomization."""

    metadata: ObjectMeta
    status: KustomizationStatus = KustomizationStatus()


@dataclass
class StateChange:
    name: str
    old_phase: KustomizationPhase | None
    new_phase: KustomizationPhase
    message: str = ""


def derive_phase(conditions: Sequence[FluxCondition]) -> KustomizationPhase:
    stalled = next((c for c in conditions if c.type == "Stalled"), None)
    if stalled and stalled.status == "True":
        return KustomizationPhase.STALLED

    ready = next((c for c in conditions if c.type == "Ready"), None)
    if ready is None:
        return KustomizationPhase.PENDING
    if ready.status == "True":
        return KustomizationPhase.READY
    if ready.status == "Unknown":
        return KustomizationPhase.RECONCILING

    # ready.status == "False"
    if ready.reason == "DependencyNotReady":
        return KustomizationPhase.DEP_WAIT

    reconciling = next((c for c in conditions if c.type == "Reconciling"), None)
    if reconciling and reconciling.status == "True":
        return KustomizationPhase.RECONCILING

    return KustomizationPhase.FAILED


def get_ready_condition(ks: FluxKustomization) -> FluxCondition | None:
    return next((c for c in ks.status.conditions if c.type == "Ready"), None)


def update_tracked_state(
    tracked: dict[str, FluxKustomization], items: Sequence[FluxKustomization]
) -> list[StateChange]:
    """Update tracked state from Flux Kustomization items, return phase changes."""
    changes: list[StateChange] = []
    for item in items:
        new_phase = derive_phase(item.status.conditions)
        old = tracked.get(item.metadata.name)
        old_phase = derive_phase(old.status.conditions) if old else None
        if old_phase != new_phase:
            ready_cond = get_ready_condition(item)
            changes.append(
                StateChange(
                    name=item.metadata.name,
                    old_phase=old_phase,
                    new_phase=new_phase,
                    message=(ready_cond.message if ready_cond else ""),
                )
            )
        tracked[item.metadata.name] = item
    return changes


def _print_changes(changes: list[StateChange], elapsed: timedelta) -> None:
    """Print batched state change lines, grouping by transition type."""
    groups: dict[tuple[KustomizationPhase | None, KustomizationPhase], list[str]] = {}
    for s in changes:
        key = (s.old_phase, s.new_phase)
        groups.setdefault(key, []).append(s.name)

    ts = timedelta(seconds=int(elapsed.total_seconds()))
    for (old, new), names in groups.items():
        transition = f"{old} -> {new}" if old else f"-> {new}"
        if len(names) <= 3:
            logger.info("%s %s: %s", ts, ", ".join(sorted(names)), transition)
        else:
            logger.info("%s %d kustomizations: %s", ts, len(names), transition)

    for s in changes:
        if s.new_phase in (KustomizationPhase.FAILED, KustomizationPhase.STALLED) and s.message:
            logger.info("        %s: %s", s.name, s.message)


def _print_summary(tracked: dict[str, FluxKustomization], elapsed: timedelta) -> None:
    counts = Counter(derive_phase(ks.status.conditions) for ks in tracked.values())
    total = len(tracked)
    ready = counts.get(KustomizationPhase.READY, 0)
    parts = [f"{ready}/{total} Ready"]
    for phase in KustomizationPhase:
        if phase == KustomizationPhase.READY:
            continue
        count = counts.get(phase, 0)
        if count > 0:
            parts.append(f"{count} {phase}")
    ts = timedelta(seconds=int(elapsed.total_seconds()))
    logger.info("%s Progress: %s", ts, ", ".join(parts))


def _print_final_summary(tracked: dict[str, FluxKustomization], *, success: bool, reason: str = "") -> None:
    if success:
        logger.info("All %d kustomizations Ready", len(tracked))
        return

    logger.error("Convergence failed: %s", reason)
    not_ready = sorted(
        (ks for ks in tracked.values() if derive_phase(ks.status.conditions) != KustomizationPhase.READY),
        key=lambda ks: ks.metadata.name,
    )
    for ks in not_ready:
        ready_cond = get_ready_condition(ks)
        phase = derive_phase(ks.status.conditions)
        logger.error(
            "  %s (%s): %s - %s",
            ks.metadata.name,
            phase,
            ready_cond.reason if ready_cond else "",
            ready_cond.message if ready_cond else "",
        )
    counts = Counter(derive_phase(ks.status.conditions) for ks in tracked.values())
    ready = counts.get(KustomizationPhase.READY, 0)
    logger.error("Summary: %d/%d Ready, %d not ready", ready, len(tracked), len(not_ready))


def monitor_flux_convergence(
    *,
    global_timeout: timedelta = timedelta(hours=1),
    poll_interval: timedelta = timedelta(seconds=10),
    stable_failure_window: timedelta = timedelta(minutes=12),
) -> None:
    """Monitor Flux kustomizations until all are Ready or convergence stalls.

    Terminates when:
    1. All kustomizations Ready (success)
    2. Ready count hasn't increased for stable_failure_window (failure)
    3. Global timeout (failure)
    """
    custom_api = client.CustomObjectsApi()

    start = datetime.now(UTC)
    tracked: dict[str, FluxKustomization] = {}
    last_ready_increase = start
    last_successful_poll = start
    high_water_ready = 0
    prev_total = 0
    total_stable_polls = 0
    last_summary_at = start - timedelta(seconds=30)

    while True:
        now = datetime.now(UTC)
        elapsed = now - start
        if elapsed >= global_timeout:
            _print_final_summary(tracked, success=False, reason=f"global timeout ({global_timeout})")
            raise SystemExit("Flux convergence timed out")

        try:
            raw = custom_api.list_namespaced_custom_object(
                group="kustomize.toolkit.fluxcd.io", version="v1", namespace="flux-system", plural="kustomizations"
            )
            last_successful_poll = datetime.now(UTC)
        except ApiException as e:
            if elapsed < timedelta(minutes=1):
                logger.debug("API not ready yet: %s", e.reason)
            else:
                logger.warning("API error polling kustomizations: %s", e.reason)
            time.sleep(poll_interval.total_seconds())
            continue

        items = [FluxKustomization.model_validate(i) for i in raw.get("items", [])]
        changes = update_tracked_state(tracked, items)

        # Track total count stability (don't declare success during ramp-up)
        if len(tracked) == prev_total:
            total_stable_polls += 1
        else:
            total_stable_polls = 0
            prev_total = len(tracked)

        # Track Ready count high-water mark for staleness detection
        ready_count = sum(
            1 for ks in tracked.values() if derive_phase(ks.status.conditions) == KustomizationPhase.READY
        )
        if ready_count > high_water_ready:
            high_water_ready = ready_count
            last_ready_increase = datetime.now(UTC)

        if changes:
            _print_changes(changes, elapsed)

        # Periodic summary every 30s
        if now - last_summary_at >= timedelta(seconds=30):
            _print_summary(tracked, elapsed)
            last_summary_at = now

        # Success: all Ready and total count stable for at least 2 polls
        if tracked and ready_count == len(tracked) and total_stable_polls >= 2:
            _print_final_summary(tracked, success=True)
            return

        # Stalled: Ready count hasn't increased for stable_failure_window
        # (only evaluate when last poll succeeded recently)
        since_increase = datetime.now(UTC) - last_ready_increase
        since_poll = datetime.now(UTC) - last_successful_poll
        if since_increase >= stable_failure_window and since_poll < poll_interval * 3:
            _print_final_summary(
                tracked,
                success=False,
                reason=f"Ready count stuck at {high_water_ready}/{len(tracked)} for {since_increase}",
            )
            raise SystemExit("Flux convergence stalled")

        time.sleep(poll_interval.total_seconds())
