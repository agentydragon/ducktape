from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import pytest_bazel
from kubernetes_asyncio import client

from cluster.skills.flux_reconcile_audit.audit import classify, collect_universe

_SINCE = datetime(2026, 1, 1, tzinfo=UTC)


def _custom_objects(**objects: list[dict]) -> client.CustomObjectsApi:
    async def list_cluster_custom_object(*, group: str, version: str, plural: str) -> dict:
        return {"items": objects.get(plural, [])}

    return MagicMock(spec=client.CustomObjectsApi, list_cluster_custom_object=list_cluster_custom_object)


@pytest.mark.parametrize(
    ("conditions", "p99", "bucket"),
    [
        ([{"type": "Ready", "status": "False", "reason": "UpgradeFailed"}], 0, "Broken"),
        ([{"type": "Ready", "status": "False", "reason": "DependencyNotReady"}], 0, "Propagating"),
        ([{"type": "Reconciling", "status": "True", "reason": "Progressing"}], 0, "Propagating"),
        ([{"type": "Ready", "status": "True"}], 301, "Slow"),
        ([{"type": "Ready", "status": "True"}], 0, "Healthy"),
        ([], 0, "Unknown"),
    ],
)
async def test_helm_release_uses_current_conditions_and_mimir(
    conditions: list[dict[str, str]], p99: float, bucket: str
) -> None:
    custom = _custom_objects(
        helmreleases=[
            {
                "metadata": {"name": "test-release", "namespace": "test-namespace"},
                "status": {
                    "conditions": conditions,
                    "history": [{"lastDeployed": "2026-01-02T00:00:00Z", "status": "deployed", "version": 12}],
                },
            }
        ]
    )
    [resource] = await collect_universe(custom)

    classify(resource, _SINCE, 300, loki_fail_supplement=3, loki_success_supplement=4, p99_mimir=p99)

    assert resource.bucket == bucket
    assert resource.evidence["history_in_window"] == 0
    assert resource.evidence["failures_attempts"] == 3
    assert resource.evidence["successes_attempts"] == 4


async def test_kustomization_uses_latest_reconcile_history() -> None:
    custom = _custom_objects(
        kustomizations=[
            {
                "metadata": {"name": "test-app", "namespace": "test-namespace"},
                "status": {
                    "history": [
                        {
                            "lastReconciled": "2026-01-02T00:00:00Z",
                            "lastReconciledDuration": "12s",
                            "lastReconciledStatus": "ReconciliationFailed",
                            "metadata": {"revision": "test-revision"},
                            "totalReconciliations": 2,
                        }
                    ]
                },
            }
        ]
    )
    [resource] = await collect_universe(custom)

    classify(resource, _SINCE, 60, loki_fail_supplement=0, loki_success_supplement=0, p99_mimir=0)

    assert resource.bucket == "Broken"
    assert resource.evidence["failures_attempts"] == 2
    assert resource.evidence["max_history_duration_s"] == 12
    assert resource.evidence["latest_revision"] == "test-revision"


if __name__ == "__main__":
    pytest_bazel.main()
