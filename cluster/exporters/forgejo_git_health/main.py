"""Prometheus exporter that detects a Forgejo replica sitting on a dead/stale SeaweedFS mount.

Kubernetes readiness (`/api/healthz`) is DB-backed and stays green while a replica's git
storage is broken (ducktape#4616). This execs `git for-each-ref` inside each Forgejo pod
against a known repository: resolving a ref's object type forces a pack file `mmap`, the
path that SIGBUSes on a stale FUSE cache -- a `git rev-parse`/`stat`/`ls` probe never
touches a pack and stayed green through the same fault during the incident.

Deliberately an alerting exporter, not a readiness probe: the fault is usually a property
of the shared storage backend and correlated across replicas, so a readiness probe
exercising it would empty every endpoint at once and turn a partial outage into a total
one (see the issue's rejected-readiness-probe discussion).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream

logger = logging.getLogger(__name__)

# exit(128 + SIGBUS). Observed when git mmaps a pack file through a dead/stale FUSE mount.
_SIGBUS_EXIT_CODE = 135


@dataclass(frozen=True)
class Settings:
    namespace: str
    pod_label_selector: str
    repo_path: str  # Absolute path to the check repo's bare .git directory inside the pod.
    exec_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> Settings:
        owner = os.environ.get("FORGEJO_HEALTH_CHECK_REPO_OWNER", "haku")
        repo = os.environ.get("FORGEJO_HEALTH_CHECK_REPO", "haku-state")
        data_path = os.environ.get("FORGEJO_DATA_PATH", "/data")
        return cls(
            namespace=os.environ.get("FORGEJO_NAMESPACE", "forgejo"),
            pod_label_selector=os.environ.get(
                "FORGEJO_POD_LABEL_SELECTOR", "app.kubernetes.io/name=forgejo,app.kubernetes.io/instance=forgejo"
            ),
            repo_path=f"{data_path}/git/repositories/{owner}/{repo}.git",
        )


@dataclass(frozen=True)
class PodCheckResult:
    pod: str
    healthy: bool
    detail: str


def classify_exec(returncode: int | None, stderr: str) -> tuple[bool, str]:
    """Turn a `git for-each-ref` exec outcome into a health verdict."""
    if returncode == 0:
        return True, "ok"
    if returncode == _SIGBUS_EXIT_CODE:
        return False, "git killed by SIGBUS reading a pack file -- dead/stale FUSE mount (ducktape#4616)"
    if returncode is None:
        return False, "exec timed out"
    return False, f"git exited {returncode}: {stderr.strip()[:200]}"


def list_forgejo_pods(core_api: Any, settings: Settings) -> list[tuple[str, str]]:
    """Return (pod name, container name) for every Running Forgejo pod.

    Lists pods directly rather than trusting the Service's Ready endpoints: a pod with a
    dead mount stays Ready (readiness is DB-backed, ducktape#4616), so an Endpoints-based
    view would silently skip exactly the pod this check exists to catch.
    """
    pods = core_api.list_namespaced_pod(namespace=settings.namespace, label_selector=settings.pod_label_selector)
    return [
        (pod.metadata.name, pod.spec.containers[0].name)
        for pod in pods.items
        if pod.status is not None and pod.status.phase == "Running"
    ]


def exec_git_check(core_api: Any, settings: Settings, pod: str, container: str) -> PodCheckResult:
    """Exec `git for-each-ref` in one pod and classify the result."""
    ws = stream(
        core_api.connect_get_namespaced_pod_exec,
        pod,
        settings.namespace,
        container=container,
        command=["git", "-C", settings.repo_path, "for-each-ref", "--count=1"],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _preload_content=False,
    )
    ws.run_forever(timeout=settings.exec_timeout_seconds)
    healthy, detail = classify_exec(ws.returncode, ws.read_stderr())
    if not healthy:
        logger.warning("Forgejo pod %s failed git health check: %s", pod, detail)
    return PodCheckResult(pod=pod, healthy=healthy, detail=detail)


def render_metrics(results: list[PodCheckResult]) -> str:
    lines = [
        "# HELP forgejo_git_health Whether `git for-each-ref` against a known repo succeeded (1) "
        "or crashed/failed (0) inside this Forgejo pod.",
        "# TYPE forgejo_git_health gauge",
    ]
    lines.extend(f'forgejo_git_health{{pod="{result.pod}"}} {int(result.healthy)}' for result in results)
    return "\n".join((*lines, "# EOF", ""))


def create_app(
    settings: Settings, core_api: Any, check_pod: Callable[[Any, Settings, str, str], PodCheckResult] = exec_git_check
) -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> Response:
        try:
            pods = list_forgejo_pods(core_api, settings)
        except ApiException as exc:
            logger.exception("Failed to list Forgejo pods")
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        results = [check_pod(core_api, settings, pod, container) for pod, container in pods]
        return Response(render_metrics(results), media_type="application/openmetrics-text; version=1.0.0")

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config.load_incluster_config()
    uvicorn.run(create_app(Settings.from_env(), client.CoreV1Api()), host="0.0.0.0", port=9173)


if __name__ == "__main__":
    main()
