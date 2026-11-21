#!/usr/bin/env python3
"""Audit Kustomization reconcile history and HelmRelease conditions.

Mimir reconcile durations and Loki log counts are optional supplements.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from kubernetes_asyncio import client, config

from cluster.validation.flux import FluxKustomizationStatus, FluxReconcileHistoryEntry, FluxStatus
from cluster.validation.k8s import K8sMetadata

logger = logging.getLogger(__name__)

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

# Probe API addresses: (group, version, plural).
DEFAULT_PROBE_KINDS: list[tuple[str, str, str]] = [
    ("apps", "v1", "deployments"),
    ("apps", "v1", "statefulsets"),
    ("apps", "v1", "daemonsets"),
    ("batch", "v1", "jobs"),
    ("batch", "v1", "cronjobs"),
    ("helm.toolkit.fluxcd.io", "v2", "helmreleases"),
    ("external-secrets.io", "v1", "externalsecrets"),
    ("postgresql.cnpg.io", "v1", "clusters"),
    ("cdi.kubevirt.io", "v1beta1", "datavolumes"),
    ("kubevirt.io", "v1", "virtualmachines"),
    ("seaweed.seaweedfs.com", "v1", "buckets"),
    ("infra.contrib.fluxcd.io", "v1alpha2", "terraforms"),
    ("cilium.io", "v2", "ciliumenvoyconfigs"),
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
    suspended: bool
    status: FluxStatus
    bucket: str = "?"
    evidence: dict = field(default_factory=dict)


async def mimir_p99_by_name(http: httpx.AsyncClient, window: str) -> dict[tuple[str, str], float]:
    promql = (
        f"histogram_quantile(0.99, sum by (le, name, kind) (rate(gotk_reconcile_duration_seconds_bucket[{window}])))"
    )
    try:
        r = await http.get(f"{MIMIR_URL}/query", params={"query": promql}, timeout=60.0)
        result = r.json()["data"]["result"]
    except httpx.HTTPError, KeyError, ValueError:
        logger.warning("Mimir query failed; p99 reconcile durations are missing from this report", exc_info=True)
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
        except KeyError, ValueError:
            continue
        if math.isnan(v):
            continue
        out[(kd, nm)] = v
    return out


async def _loki_count_by_name(
    http: httpx.AsyncClient, app: str, name_label: str, line_match: str, window: str
) -> dict[str, int]:
    logql = (
        f"sum by ({name_label}) (count_over_time("
        f'{{namespace="flux-system",app="{app}"}} | json | __error__="" '
        f'|~ "{line_match}" [{window}]))'
    )
    try:
        response = await http.get(f"{LOKI_URL}/query", params={"query": logql}, timeout=90.0)
        samples = response.json()["data"]["result"]
    except httpx.HTTPError, KeyError, ValueError:
        logger.warning("Loki query failed; its reconcile counts are missing from this report", exc_info=True)
        return {}
    out: dict[str, int] = {}
    for series in samples:
        nm = series.get("metric", {}).get(name_label)
        if not nm:
            continue
        try:
            out[nm] = int(float(series["value"][1]))
        except KeyError, ValueError:
            continue
    return out


async def loki_reconcile_counts(http: httpx.AsyncClient, window: str) -> dict[tuple[str, str], dict[str, int]]:
    queries = {
        (kind, outcome): _loki_count_by_name(http, app, f"{kind}_name", match, window)
        for kind, app in (("Kustomization", "kustomize-controller"), ("HelmRelease", "helm-controller"))
        for outcome, match in (("fail", "Reconciliation failed"), ("ok", "Reconciliation finished"))
    }
    return dict(zip(queries, await asyncio.gather(*queries.values()), strict=True))


async def collect_universe(custom: client.CustomObjectsApi) -> list[Resource]:
    out: list[Resource] = []
    flux_kinds = [
        ("Kustomization", "kustomize.toolkit.fluxcd.io", "v1", "kustomizations"),
        ("HelmRelease", "helm.toolkit.fluxcd.io", "v2", "helmreleases"),
    ]
    responses = await asyncio.gather(
        *(
            custom.list_cluster_custom_object(group=group, version=version, plural=plural)
            for _, group, version, plural in flux_kinds
        )
    )
    for (kind, _, _, _), resp in zip(flux_kinds, responses, strict=True):
        status_model = FluxKustomizationStatus if kind == "Kustomization" else FluxStatus
        for it in resp.get("items", []):
            md = K8sMetadata.model_validate(it.get("metadata", {}))
            spec = it.get("spec", {}) or {}
            status = status_model.model_validate(it.get("status") or {})
            out.append(
                Resource(
                    kind=kind,
                    namespace=md.namespace,
                    name=md.name,
                    suspended=bool(spec.get("suspend", False)),
                    status=status,
                )
            )
    return out


async def fetch_probe_kinds(custom: client.CustomObjectsApi) -> dict[str, list[dict]]:
    async def _fetch(group: str, version: str, plural: str) -> list[dict]:
        try:
            response = await custom.list_cluster_custom_object(group=group, version=version, plural=plural)
            return response.get("items") or []
        except client.ApiException, OSError:
            logger.warning(
                "Listing %s.%s failed; its objects are missing from the probes", plural, group, exc_info=True
            )
            return []

    results = await asyncio.gather(*(_fetch(*address) for address in DEFAULT_PROBE_KINDS))
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

    status = r.status
    kustomization = status if isinstance(status, FluxKustomizationStatus) else None
    history_in = kustomization.history_in_window(since) if kustomization else []
    latest = kustomization.latest_history if kustomization else None
    success_entries = [e for e in history_in if e.last_reconciled_status in SUCCESS_STATUSES]
    failure_entries = [e for e in history_in if e.last_reconciled_status in FAILURE_STATUSES]

    # A revision can have separate success and failure entries for different build digests.
    successful_revisions = {e.revision for e in success_entries}
    failed_revisions_with_recovery = {e.revision for e in failure_entries} & successful_revisions

    successes_attempts = max(sum(e.total_reconciliations for e in success_entries), loki_success_supplement)
    failures_attempts = max(sum(e.total_reconciliations for e in failure_entries), loki_fail_supplement)

    last_was_failure = latest is not None and latest.last_reconciled_status in FAILURE_STATUSES

    max_history_duration_s = max((e.duration_s for e in history_in), default=0.0)
    duration_s = p99_mimir if p99_mimir > 0 else max_history_duration_s

    # Flux history omits error messages. The current Ready condition can name the culprit.
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
        "duration_s": duration_s,
        "max_history_duration_s": max_history_duration_s,
        "p99_mimir": p99_mimir,
        "loki_fail_supplement": loki_fail_supplement,
        "loki_success_supplement": loki_success_supplement,
        "latest_status": latest.last_reconciled_status if latest else None,
        "latest_revision": latest.revision if latest else None,
        "failed_revisions_with_recovery": sorted(failed_revisions_with_recovery),
        "culprit": culprit,
        "last_error": last_error,
    }

    if kustomization is None:
        stalled = status.condition("Stalled")
        reconciling = status.condition("Reconciling")
        if stalled and stalled.status == "True":
            r.bucket = "Broken"
            r.evidence["last_error"] = stalled.message or last_error
        elif ready and ready.status == "False":
            waiting = ready.reason in {"DependencyNotReady", "SourceNotReady", "Progressing", "HealthCheckCanceled"}
            r.bucket = "Propagating" if waiting else "Broken"
        elif reconciling and reconciling.status == "True":
            r.bucket = "Propagating"
        elif not ready or ready.status != "True":
            r.bucket = "Unknown"
        elif p99_mimir > 0 and p99_mimir >= slow_threshold_s:
            r.bucket = "Slow"
        else:
            r.bucket = "Healthy"
        if r.bucket == "Propagating":
            r.evidence.update(culprit=None, last_error=None)
        return

    if last_was_failure:
        r.bucket = "Broken"
    elif ready and ready.status == "False" and ready.reason == "DependencyNotReady" and not failure_entries:
        r.bucket = "Propagating"
    elif failure_entries:
        r.bucket = "Miswired"
    elif success_entries and duration_s >= slow_threshold_s:
        r.bucket = "Slow"
    else:
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
        rev = e.revision.split(":", 1)[-1][:10]
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
    sources = ["Kustomization reconcile history", "Flux conditions"]
    if use_mimir:
        sources.append("Mimir (p99 reconcile duration)")
    if use_loki:
        sources.append("Loki (long-window count fallback)")
    sources.append("label-selector probes")
    print(f"Data sources: {', '.join(sources)}\n")
    print(
        f"Universe: {len(rs)} resources "
        f"({sum(1 for r in rs if r.kind == 'Kustomization')} Kustomizations, "
        f"{sum(1 for r in rs if r.kind == 'HelmRelease')} HelmReleases)\n"
    )
    sections = {
        "Broken": "Broken",
        "Miswired": "Miswired but converges",
        "Slow": "Slow but converges",
        "Propagating": "Propagating (transient)",
        "Unknown": "Unknown",
        "Suspended": "Suspended (informational)",
    }
    print("| Bucket | Count |")
    print("|---|---:|")
    for bucket in (*sections, "Healthy"):
        print(f"| {bucket} | {len(by_bucket[bucket])} |")
    print()

    for bucket, header in sections.items():
        items = sorted(
            by_bucket[bucket],
            key=lambda r: (-r.evidence.get("failures_attempts", 0), -r.evidence.get("duration_s", 0.0)),
        )
        if not items:
            continue
        print(f"\n## {header} ({len(items)})\n")
        for r in items:
            ev = r.evidence
            ready = r.status.ready
            print(f"### {r.kind}/{r.name} (ns={r.namespace})\n")
            if ready:
                print(f"- Current: Ready={ready.status}, reason=`{ready.reason or ''}`")
            if ev.get("p99_mimir", 0) > 0:
                print(f"- p99 reconcile duration: {ev['p99_mimir']:.1f}s (Mimir)")
            elif ev.get("max_history_duration_s", 0) > 0:
                print(
                    f"- Longest recorded reconcile duration: {ev['max_history_duration_s']:.1f}s (Kustomization history)"
                )
            if ev.get("failures_attempts") or ev.get("successes_attempts"):
                extras = []
                if ev.get("loki_fail_supplement"):
                    extras.append(f"loki-fail={ev['loki_fail_supplement']}")
                if ev.get("loki_success_supplement"):
                    extras.append(f"loki-ok={ev['loki_success_supplement']}")
                tag = f" ({', '.join(extras)})" if extras else ""
                entries = ""
                if isinstance(r.status, FluxKustomizationStatus):
                    entries = f" across {ev['failure_entries']}+{ev['success_entries']} history entries"
                print(
                    f"- Reconcile attempts in window: "
                    f"{ev['failures_attempts']} failed / "
                    f"{ev['successes_attempts']} succeeded"
                    f"{entries}{tag}"
                )
            if ev.get("latest_status"):
                rev = ev.get("latest_revision") or ""
                rev_short = rev.split(":", 1)[-1][:10]
                print(f"- Latest entry: `{ev['latest_status']}` at rev `{rev_short}`")
            if ev.get("failed_revisions_with_recovery"):
                revs = ", ".join(rev.split(":", 1)[-1][:10] for rev in ev["failed_revisions_with_recovery"][:3])
                print(f"- Revisions that failed then recovered: {revs}")
            if ev.get("culprit"):
                k, ns, nm, st = ev["culprit"]
                print(f"- Underlying culprit (from condition): {k}/{nm} ({ns}) — status `{st}`")
            if ev.get("last_error"):
                err = ev["last_error"]
                if len(err) > 400:
                    err = err[:400] + "…"
                print(f"- Last error: `{err}`")
            if isinstance(r.status, FluxKustomizationStatus) and r.status.history:
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

        universe = asyncio.create_task(collect_universe(custom))
        probes = asyncio.create_task(fetch_probe_kinds(custom))
        mimir = asyncio.create_task(mimir_p99_by_name(http, args.window)) if use_mimir else None
        loki = asyncio.create_task(loki_reconcile_counts(http, args.window)) if use_loki else None
        rs, probe_objs = await universe, await probes
        p99_map = await mimir if mimir else {}
        loki_counts = await loki if loki else {}

    if args.name:
        rs = [r for r in rs if r.name == args.name]

    for r in rs:
        thr = args.slow_kustomization_s if r.kind == "Kustomization" else args.slow_helmrelease_s
        f_supp = loki_counts.get((r.kind, "fail"), {}).get(r.name, 0)
        ok_supp = loki_counts.get((r.kind, "ok"), {}).get(r.name, 0)
        p99 = p99_map.get((r.kind, r.name), 0.0)
        try:
            classify(r, since, thr, f_supp, ok_supp, p99)
        except Exception as e:
            r.bucket = "?Error"
            r.evidence = {"error": repr(e)}

    emit_report(rs, args.window, use_mimir, use_loki, probe_objs)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="7d")
    ap.add_argument("--name", default=None)
    ap.add_argument(
        "--include-mimir", action="store_true", help="Pull p99 reconcile duration from Mimir for both resource kinds."
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
