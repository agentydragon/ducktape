"""The clickhouse Namespace, written into the directory of the Kustomization that owns it."""

from __future__ import annotations

from pathlib import Path

from cluster.cdk8s.generation import write_namespace


def write_manifests(root: Path) -> None:
    write_namespace(
        root,
        "cluster/k8s/clickhouse/operator",
        name="clickhouse",
        labels={
            "goldilocks.fairwinds.com/enabled": "true",
            # ClickHouse and Keeper have topology-aware, capacity-planned requests.
            # VPA admission raised ClickHouse from 500m to 1554m, which made its
            # local-PV-pinned replica unschedulable. Keep recommendations visible in
            # Goldilocks without mutating operator-managed Pods.
            "goldilocks.fairwinds.com/vpa-update-mode": "off",
            "rbac.ducktape.io/agent-readable-logs": "true",
            "pod-security.kubernetes.io/enforce": "baseline",
            "pod-security.kubernetes.io/audit": "restricted",
            "pod-security.kubernetes.io/warn": "restricted",
        },
    )
