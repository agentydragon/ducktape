#!/usr/bin/env python3
"""Flux reconcile audit (v5 — history-first).

Walks every Flux Kustomization + HelmRelease, classifies each into one of
Broken / Slow-but-converges / Miswired-but-converges / Propagating /
Suspended / Healthy. Primary signal is the per-revision `status.history`
shipped by Flux 2.7+; Mimir / Loki / Events are optional supplements.

Default mode reads only the live K8s API and runs in ~2-3 seconds.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from kubernetes_asyncio import client, config

from cluster.validation.flux import FluxKustomizationStatus, FluxReconcileHistoryEntry
from cluster.validation.k8s import K8sMetadata

MIMIR_URL = "http://localhost:8080/prometheus/api/v1"
LOKI_URL = "http://localhost:3100/loki/api/v1"

SUCCESS_STATUSES = {"ReconciliationSucceeded", "InstallSucceeded", "UpgradeSucceeded"}
FAILURE_STATUSES = {
    "ReconciliationFailed",
    "HealthCheckFailed",
    "BuildFailed",
    "PostBuildFailed",
    "ApplyFailed",
    "PruneFailed",
    "DecryptionFailed",
    "ValidationFailed",
    "AccessDenied",
    "InstallFailed",
    "UpgradeFailed",
    "RollbackFailed",
    "TestFailed",
    "ChartPullFailed",
}

CULPRIT_RE = re.compile(r"\[(\w+)/([\w\-.]+)/([\w\-.]+) status: '([^']+)'\]")

# Probe targets: (group, version, plural, Kind).
DEFAULT_PROBE_KINDS: list[tuple[str, str, str, str]] = [
    ("apps", "v1", "deployments", "Deployment"),
    ("apps", "v1", "statefulsets", "StatefulSet"),
    ("apps", "v1", "daemonsets", "DaemonSet"),
    ("batch", "v1", "jobs", "Job"),
    ("batch", "v1", "cronjobs", "CronJob"),
    ("helm.toolkit.fluxcd.io", "v2", "helmreleases", "HelmRelease"),
    ("external-secrets.io", "v1", "externalsecrets", "ExternalSecret"),
    ("postgresql.cnpg.io", "v1", "clusters", "Cluster"),
    ("cdi.kubevirt.io", "v1beta1", "datavolumes", "DataVolume"),
    ("kubevirt.io", "v1", "virtualmachines", "VirtualMachine"),
    ("seaweed.seaweedfs.com", "v1", "buckets", "Bucket"),
    ("infra.contrib.fluxcd.io", "v1alpha2", "terraforms", "Terraform"),
    ("openclaw.rocks", "v1alpha1", "openclawinstances", "OpenclawInstance"),
    ("cilium.io", "v2", "ciliumenvoyconfigs", "CiliumEnvoyConfig"),
]

FLUX_LABEL = "kustomize.toolkit.fluxcd.io/name"


def parse_window_seconds(window: str) -> int:
    m = re.fullmatch(r"(\d+)([smhdw])", window)
    if not m:
        raise ValueError(f"bad window: {window!r}")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


@dataclass
class Resource:
    kind: str  # "Kustomization" | "HelmRelease"
    namespace: str
    name: str
    api_version: str
    suspended: bool
    status: FluxKustomizationStatus
    bucket: str = "?"
    evidence: dict = field(default_factory=dict)


async def mimir_p99_by_name(http: httpx.AsyncClient, window: str) -> dict[tuple[str, str], float]:
    promql = (
        f"histogram_quantile(0.99, sum by (le, name, kind) (rate(gotk_reconcile_duration_seconds_bucket[{window}])))"
    )
    try:
        r = await http.get(f"{MIMIR_URL}/query", params={"query": promql}, timeout=60.0)
        result = r.json()["data"]["result"]
    except Exception:
        return {}
    out: dict[tuple[str, str], float] = {}
    for series in result:
        m = series.get("metric", {})
        nm = m.get("name")
        kd = m.get("kind")
        if not nm or not kd:
            continue
        try:
            v = float(series["value"][1])
        except (KeyError, ValueError):
            continue
        if math.isnan(v):
            continue
        out[(kd, nm)] = v
    return out


async def loki_count_by_name(
    http: httpx.AsyncClient, app: str, name_label: str, line_match: str, window: str
) -> dict[str, int]:
    promql = (
        f"sum by ({name_label}) (count_over_time("
        f'{{namespace="flux-system",app="{app}"}} | json | __error__="" '
        f'|~ "{line_match}" [{window}]))'
    )
    try:
        r = await http.get(f"{LOKI_URL}/query", params={"query": promql}, timeout=90.0)
        result = r.json()["data"]["result"]
    except Exception:
        return {}
    out: dict[str, int] = {}
    for series in result:
        nm = series.get("metric", {}).get(name_label)
        if not nm:
            continue
        try:
            out[nm] = int(float(series["value"][1]))
        except (KeyError, ValueError):
            continue
    return out


async def collect_universe(custom: client.CustomObjectsApi) -> list[Resource]:
    out: list[Resource] = []
    flux_kinds = [
        ("Kustomization", "kustomize.toolkit.fluxcd.io/v1", "kustomize.toolkit.fluxcd.io", "v1", "kustomizations"),
        ("HelmRelease", "helm.toolkit.fluxcd.io/v2", "helm.toolkit.fluxcd.io", "v2", "helmreleases"),
    ]
    responses = await asyncio.gather(
        *(
            custom.list_cluster_custom_object(group=group, version=version, plural=plural)
            for _, _, group, version, plural in flux_kinds
        )
    )
    for (kind, api_version, _, _, _), resp in zip(flux_kinds, responses, strict=True):
        for it in resp.get("items", []):
            md = K8sMetadata.model_validate(it.get("metadata", {}))
            spec = it.get("spec", {}) or {}
            status = FluxKustomizationStatus.model_validate(it.get("status", {}) or {})
            out.append(
                Resource(
                    kind=kind,
                    namespace=md.namespace,
                    name=md.name,
                    api_version=api_version,
                    suspended=bool(spec.get("suspend", False)),
                    status=status,
                )
            )
    return out


async def fetch_probe_kinds(
    custom: client.CustomObjectsApi, probe_kinds: list[tuple[str, str, str, str]]
) -> dict[str, list[dict]]:
    async def _fetch(spec: tuple[str, str, str, str]) -> list[dict]:
        try:
            return (await custom.list_cluster_custom_object(group=spec[0], version=spec[1], plural=spec[2])).get(
                "items", []
            ) or []
        except Exception:
            return []

    results = await asyncio.gather(*(_fetch(s) for s in probe_kinds))
    by_kustomization: dict[str, list[dict]] = defaultdict(list)
    for items in results:
        for obj in items:
            labels = (obj.get("metadata") or {}).get("labels") or {}
            ks = labels.get(FLUX_LABEL)
            if ks:
                by_kustomization[ks].append(obj)
    return by_kustomization


def extract_culprit(text: str) -> tuple[str, str, str, str] | None:
    m = CULPRIT_RE.search(text or "")
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def classify(
    r: Resource,
    since: datetime,
    slow_threshold_s: float,
    loki_fail_supplement: int,
    loki_success_supplement: int,
    p99_mimir: float,
) -> None:
    if r.suspended:
        r.bucket = "Suspended"
        return

    history_in = r.status.history_in_window(since)
    latest = r.status.latest_history
    success_entries = [e for e in history_in if e.last_reconciled_status in SUCCESS_STATUSES]
    failure_entries = [e for e in history_in if e.last_reconciled_status in FAILURE_STATUSES]

    # Per-revision rollup. Each history entry IS per-revision-digest already;
    # we use it to drive the Miswired ("same revision: failed then succeeded
    # later" or "had failure entries then a success entry") and Broken
    # decisions. Counts are by *entry*, not by individual reconcile attempt.
    successful_revisions = {e.revision for e in success_entries}
    failed_revisions_with_recovery = {e.revision for e in failure_entries if e.revision in successful_revisions}

    # Per-history-entry attempt counts (best precision we have without Loki).
    successes_attempts = sum(e.total_reconciliations for e in success_entries)
    failures_attempts = sum(e.total_reconciliations for e in failure_entries)
    failures_attempts = max(failures_attempts, loki_fail_supplement)
    successes_attempts = max(successes_attempts, loki_success_supplement)

    last_was_failure = latest is not None and latest.last_reconciled_status in FAILURE_STATUSES

    # Max in-window history duration is a proxy for p99 — we only have
    # `lastReconciledDuration` per revision, not the full distribution. If
    # Mimir has a real histogram-derived p99 we prefer that.
    max_history_duration_s = max((e.duration_s for e in history_in), default=0.0)
    p99 = p99_mimir if p99_mimir > 0 else max_history_duration_s

    # Underlying-resource attribution. Latest history doesn't carry the
    # error message, so we fall back to the current `Ready` condition's
    # message which Flux populates with the bracketed reference.
    culprit = None
    last_error = None
    ready = r.status.ready
    if ready and ready.status == "False":
        last_error = ready.message
        culprit = extract_culprit(ready.message or "")

    r.evidence = {
        "history_in_window": len(history_in),
        "success_entries": len(success_entries),
        "failure_entries": len(failure_entries),
        "successes_attempts": successes_attempts,
        "failures_attempts": failures_attempts,
        "p99_s": p99,
        "max_history_duration_s": max_history_duration_s,
        "p99_mimir": p99_mimir,
        "loki_fail_supplement": loki_fail_supplement,
        "loki_success_supplement": loki_success_supplement,
        "last_was_failure": last_was_failure,
        "latest_status": latest.last_reconciled_status if latest else None,
        "latest_revision": latest.revision if latest else None,
        "failed_revisions_with_recovery": sorted(failed_revisions_with_recovery),
        "culprit": culprit,
        "last_error": last_error,
    }

    # Classification (first match wins).
    #
    # Broken: latest history entry has a failure status. The controller is
    # currently stuck at this revision.
    if last_was_failure:
        r.bucket = "Broken"
        return

    # Propagating: currently Ready=False because a dependsOn target is still
    # catching up. No real failures in history means the resource is in the
    # transient propagation state, not broken.
    if ready and ready.status == "False" and ready.reason == "DependencyNotReady" and len(failure_entries) == 0:
        r.bucket = "Propagating"
        return

    # Miswired: latest is a success but earlier entries in the window
    # failed — Flux retried until it converged. Covers both "same revision
    # had failures then succeeded" and "earlier revision failed before the
    # current healthy one".
    if len(failure_entries) > 0:
        r.bucket = "Miswired"
        return

    # Slow: succeeded but took too long. Threshold is per-kind.
    if len(success_entries) > 0 and p99 >= slow_threshold_s:
        r.bucket = "Slow"
        return

    r.bucket = "Healthy"


def _cond_str(c: dict) -> str:
    st = c.get("status", "?")
    reason = c.get("reason", "")
    return f"{c.get('type', '?')}={st}{(' ' + reason) if reason else ''}"


def _summarize_status(kind: str, obj: dict) -> str:
    status = obj.get("status", {}) or {}
    conds = status.get("conditions") or []
    by_type = {c.get("type"): c for c in conds}

    if kind in {"Deployment", "ReplicaSet"}:
        parts = [_cond_str(by_type[t]) for t in ("Available", "Progressing") if t in by_type]
        if parts:
            return ", ".join(parts)

    pref = {"Pod": "Ready", "Job": "Complete", "Node": "Ready"}.get(kind, "Ready")
    chosen = by_type.get(pref) or by_type.get("Ready") or by_type.get("Available")
    if chosen:
        return _cond_str(chosen)
    if status.get("phase"):
        return f"phase={status['phase']}"
    return "?"


def _is_unhealthy(summary: str) -> bool:
    if not summary or summary == "?":
        return True
    if "=False" in summary:
        return True
    if summary.startswith("phase="):
        return summary not in {"phase=Succeeded", "phase=Running", "phase=Bound"}
    return False


def probe_managed_objects(r: Resource, probe_objs: dict[str, list[dict]]) -> list[str]:
    if r.kind != "Kustomization":
        return []
    out: list[str] = []
    for obj in probe_objs.get(r.name, []):
        kind = obj.get("kind", "")
        md = obj.get("metadata", {}) or {}
        summary = _summarize_status(kind, obj)
        if _is_unhealthy(summary):
            out.append(f"{kind}/{md.get('name', '')} ({md.get('namespace', '')}): {summary}")
            if len(out) >= 8:
                break
    return out


def _format_history_table(history: list[FluxReconcileHistoryEntry], n: int = 5) -> str:
    """One-line per entry summary, most-recent first."""
    sorted_h = sorted(
        (e for e in history if e.last_reconciled),
        key=lambda e: e.last_reconciled,  # type: ignore[arg-type,return-value]
        reverse=True,
    )
    lines = []
    for e in sorted_h[:n]:
        rev = e.revision.split(":", 1)[-1][:10] if ":" in e.revision else e.revision[:10]
        ts = e.last_reconciled.isoformat(timespec="seconds") if e.last_reconciled else "?"
        lines.append(
            f"  - {ts} `{e.last_reconciled_status}` rev=`{rev}` total={e.total_reconciliations} dur={e.duration_s:.1f}s"
        )
    return "\n".join(lines)


def emit_report(
    rs: list[Resource], window: str, use_mimir: bool, use_loki: bool, probe_objs: dict[str, list[dict]]
) -> None:
    by_bucket = defaultdict(list)
    for r in rs:
        by_bucket[r.bucket].append(r)

    print(f"# Flux Reconcile Audit — last {window}\n")
    sources = ["status.history"]
    if use_mimir:
        sources.append("Mimir (p99 fallback)")
    if use_loki:
        sources.append("Loki (long-window count fallback)")
    sources.append("label-selector probes")
    print(f"Data sources: {', '.join(sources)}\n")
    print(
        f"Universe: {len(rs)} resources "
        f"({sum(1 for r in rs if r.kind == 'Kustomization')} Kustomizations, "
        f"{sum(1 for r in rs if r.kind == 'HelmRelease')} HelmReleases)\n"
    )
    print("| Bucket | Count |")
    print("|---|---:|")
    for b in ("Broken", "Miswired", "Slow", "Propagating", "Suspended", "Healthy"):
        print(f"| {b} | {len(by_bucket[b])} |")
    print()

    for b, header in [
        ("Broken", "## Broken"),
        ("Miswired", "## Miswired but converges"),
        ("Slow", "## Slow but converges"),
        ("Propagating", "## Propagating (transient)"),
        ("Suspended", "## Suspended (informational)"),
    ]:
        items = sorted(
            by_bucket[b], key=lambda r: (-r.evidence.get("failures_attempts", 0), -r.evidence.get("p99_s", 0.0))
        )
        if not items:
            continue
        print(f"\n{header} ({len(items)})\n")
        for r in items:
            ev = r.evidence
            ready = r.status.ready
            print(f"### {r.kind}/{r.name} (ns={r.namespace})\n")
            if ready:
                print(f"- Current: Ready={ready.status}, reason=`{ready.reason or ''}`")
            if ev.get("p99_s", 0) > 0:
                src = "Mimir" if ev.get("p99_mimir", 0) > 0 else "history-max"
                print(f"- p99 reconcile duration: {ev['p99_s']:.1f}s ({src})")
            if ev.get("failures_attempts") or ev.get("successes_attempts"):
                extras = []
                if ev.get("loki_fail_supplement"):
                    extras.append(f"loki-fail={ev['loki_fail_supplement']}")
                if ev.get("loki_success_supplement"):
                    extras.append(f"loki-ok={ev['loki_success_supplement']}")
                tag = f" ({', '.join(extras)})" if extras else ""
                print(
                    f"- Reconcile attempts in window: "
                    f"{ev['failures_attempts']} failed / "
                    f"{ev['successes_attempts']} succeeded"
                    f" across {ev['failure_entries']}+{ev['success_entries']} history entries{tag}"
                )
            if ev.get("latest_status"):
                rev = ev.get("latest_revision") or ""
                rev_short = rev.split(":", 1)[-1][:10] if ":" in rev else rev[:10]
                print(f"- Latest entry: `{ev['latest_status']}` at rev `{rev_short}`")
            if ev.get("failed_revisions_with_recovery"):
                revs = ", ".join(
                    (r.split(":", 1)[-1][:10] if ":" in r else r[:10]) for r in ev["failed_revisions_with_recovery"][:3]
                )
                print(f"- Revisions that failed then recovered: {revs}")
            if ev.get("culprit"):
                k, ns, nm, st = ev["culprit"]
                print(f"- Underlying culprit (from condition): {k}/{nm} ({ns}) — status `{st}`")
            if ev.get("last_error"):
                err = ev["last_error"]
                if len(err) > 400:
                    err = err[:400] + "…"
                print(f"- Last error: `{err}`")
            if r.status.history:
                print("- History (most recent first):")
                print(_format_history_table(r.status.history))
            lines = probe_managed_objects(r, probe_objs)
            if lines:
                print("- Label-selector probe (unhealthy managed objects):")
                for line in lines:
                    print(f"  - {line}")
            print()

    healthy = len(by_bucket["Healthy"])
    print(f"\n## Healthy ({healthy})\n\n{healthy} resources passed all checks.\n")


async def _empty_dict() -> dict:
    return {}


async def async_main(args: argparse.Namespace) -> None:
    window_s = parse_window_seconds(args.window)
    since = datetime.now(UTC) - timedelta(seconds=window_s)
    use_mimir = args.include_mimir
    use_loki = args.include_loki and window_s > 3600

    try:
        await config.load_kube_config()
    except config.ConfigException:
        config.load_incluster_config()

    async with client.ApiClient() as api, httpx.AsyncClient() as http:
        custom = client.CustomObjectsApi(api)

        loki_specs: list[tuple[str, str]] = []
        loki_coros = []
        if use_loki:
            for kind, app, name_label in [
                ("Kustomization", "kustomize-controller", "Kustomization_name"),
                ("HelmRelease", "helm-controller", "HelmRelease_name"),
            ]:
                for which, match in (("fail", "Reconciliation failed"), ("ok", "Reconciliation finished")):
                    loki_specs.append((kind, which))
                    loki_coros.append(loki_count_by_name(http, app, name_label, match, args.window))

        f_universe = asyncio.create_task(collect_universe(custom))
        f_probe = asyncio.create_task(fetch_probe_kinds(custom, DEFAULT_PROBE_KINDS))
        f_mimir = asyncio.create_task(mimir_p99_by_name(http, args.window) if use_mimir else _empty_dict())
        f_loki = [asyncio.create_task(c) for c in loki_coros] if loki_coros else []

        rs = await f_universe
        probe_objs = await f_probe
        p99_map = await f_mimir
        loki_results = [await c for c in f_loki]

    if args.name:
        rs = [r for r in rs if r.name == args.name]

    loki_fail: dict[tuple[str, str], int] = {}
    loki_ok: dict[tuple[str, str], int] = {}
    for (kind, which), counts in zip(loki_specs, loki_results, strict=True):
        target = loki_fail if which == "fail" else loki_ok
        for nm, cnt in counts.items():
            target[(kind, nm)] = cnt

    for r in rs:
        thr = args.slow_kustomization_s if r.kind == "Kustomization" else args.slow_helmrelease_s
        f_supp = loki_fail.get((r.kind, r.name), 0)
        ok_supp = loki_ok.get((r.kind, r.name), 0)
        p99 = p99_map.get((r.kind, r.name), 0.0)
        try:
            classify(r, since, thr, f_supp, ok_supp, p99)
        except Exception as e:
            r.bucket = "?Error"
            r.evidence = {"error": repr(e)}

    emit_report(rs, args.window, use_mimir, use_loki, probe_objs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="7d")
    ap.add_argument("--name", default=None)
    ap.add_argument(
        "--include-mimir",
        action="store_true",
        help="Also pull p99 reconcile duration from Mimir (default: history-max only).",
    )
    ap.add_argument(
        "--include-loki",
        action="store_true",
        help="Also pull fail/success counts from Loki, capped at the window. Useful when "
        "the window exceeds Flux's history retention (default 10 entries).",
    )
    ap.add_argument("--slow-kustomization-s", type=float, default=60.0)
    ap.add_argument("--slow-helmrelease-s", type=float, default=300.0)
    args = ap.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
