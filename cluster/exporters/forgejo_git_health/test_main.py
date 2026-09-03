"""Tests for the Forgejo git health exporter."""

from types import SimpleNamespace

import pytest
import pytest_bazel
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException

from cluster.exporters.forgejo_git_health.main import (
    PodCheckResult,
    Settings,
    classify_exec,
    create_app,
    list_forgejo_pods,
    render_metrics,
)

_SETTINGS = Settings(
    namespace="forgejo",
    pod_label_selector="app.kubernetes.io/name=forgejo,app.kubernetes.io/instance=forgejo",
    repo_path="/data/git/repositories/haku/haku-state.git",
)


def _pod(name: str, *, phase: str = "Running", container: str = "forgejo") -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(containers=[SimpleNamespace(name=container)]),
        status=SimpleNamespace(phase=phase),
    )


class _FakeCoreV1:
    def __init__(self, pods: list[SimpleNamespace]):
        self._pods = pods

    def list_namespaced_pod(self, namespace: str, label_selector: str) -> SimpleNamespace:
        assert (namespace, label_selector) == (_SETTINGS.namespace, _SETTINGS.pod_label_selector)
        return SimpleNamespace(items=self._pods)


class _BrokenCoreV1:
    def list_namespaced_pod(self, namespace: str, label_selector: str) -> SimpleNamespace:
        raise ApiException(status=500)


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected_healthy", "detail_substring"),
    [
        (0, "", True, "ok"),
        (135, "", False, "SIGBUS"),
        (None, "", False, "timed out"),
        (1, "fatal: not a git repository", False, "fatal: not a git repository"),
    ],
)
def test_classify_exec(returncode: int | None, stderr: str, expected_healthy: bool, detail_substring: str) -> None:
    healthy, detail = classify_exec(returncode, stderr)
    assert healthy is expected_healthy
    assert detail_substring in detail


def test_list_forgejo_pods_skips_non_running_pods() -> None:
    core_api = _FakeCoreV1([_pod("forgejo-0"), _pod("forgejo-1", phase="Pending")])
    assert list_forgejo_pods(core_api, _SETTINGS) == [("forgejo-0", "forgejo")]


def test_render_metrics_reports_each_pod() -> None:
    text = render_metrics(
        [
            PodCheckResult(pod="forgejo-0", healthy=True, detail="ok"),
            PodCheckResult(pod="forgejo-1", healthy=False, detail="git exited 135"),
        ]
    )
    assert 'forgejo_git_health{pod="forgejo-0"} 1' in text
    assert 'forgejo_git_health{pod="forgejo-1"} 0' in text


def test_metrics_endpoint_checks_every_running_pod() -> None:
    core_api = _FakeCoreV1([_pod("forgejo-0"), _pod("forgejo-1")])
    checked: list[str] = []

    def fake_check(core_api: object, settings: Settings, pod: str, container: str) -> PodCheckResult:
        checked.append(pod)
        return PodCheckResult(pod=pod, healthy=pod == "forgejo-0", detail="stub")

    app = create_app(_SETTINGS, core_api, check_pod=fake_check)
    with TestClient(app) as test_client:
        response = test_client.get("/metrics")

    assert response.status_code == 200
    assert checked == ["forgejo-0", "forgejo-1"]
    assert 'forgejo_git_health{pod="forgejo-0"} 1' in response.text
    assert 'forgejo_git_health{pod="forgejo-1"} 0' in response.text


def test_metrics_endpoint_fails_when_pod_listing_errors() -> None:
    app = create_app(_SETTINGS, _BrokenCoreV1())
    with TestClient(app) as test_client:
        response = test_client.get("/metrics")

    assert response.status_code == 502


if __name__ == "__main__":
    pytest_bazel.main()
