"""The staged Go cutover keeps the old addresses and restricts each listener."""

from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import Chart, Testing as Cdk8sTesting
from more_itertools import one

from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.agentplane import (
    app,
    notifications,
    runner_gateway,
    runner_gateway_go,
    sandbox_controller,
    session_dispatcher,
    staging,
)


def _resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _go_objects() -> list[dict[str, Any]]:
    env = staging.ENV
    chart = Chart(Cdk8sTesting.app(), "candidate", disable_resource_name_hashes=True)
    callers = {
        ServiceAccountRef(namespace=env.namespace, name=app.NAME): app.service(env.namespace).pods,
        ServiceAccountRef(namespace=env.namespace, name=notifications.NAME): notifications.service(env.namespace).pods,
    }
    runner_gateway_go.GoRunnerGateway(
        chart,
        "runner-gateway",
        env,
        dispatcher_callers=callers,
        gateway_callers=callers,
        wake_callers=frozenset(callers),
        sandbox_controller=(
            ServiceAccountRef(namespace=env.namespace, name=sandbox_controller.NAME),
            sandbox_controller.pods(env.namespace),
        ),
    )
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))


def test_go_gateway_preserves_services_and_fences_listener_ingress() -> None:
    docs = _go_objects()
    deployment = _resource(docs, "Deployment", runner_gateway.NAME)
    pod = deployment["spec"]["template"]["spec"]
    container = one(c for c in pod["containers"] if c["name"] == "runner-gateway")
    assert container["image"].startswith("git.allegedly.works/ducktape-ci/agentplane-runner-gateway-go:")
    assert pod["serviceAccountName"] == runner_gateway.NAME
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert {p["containerPort"] for p in container["ports"]} == {8080, 8081, 8082}
    assert f"--dispatcher-audience={session_dispatcher.TOKEN_AUDIENCE}" in container["args"]
    assert f"--gateway-audience={runner_gateway.TOKEN_AUDIENCE}" in container["args"]
    assert f"--dispatcher-caller={staging.ENV.namespace}/{app.NAME}" in container["args"]
    assert f"--dispatcher-caller={staging.ENV.namespace}/{notifications.NAME}" in container["args"]
    assert f"--gateway-caller={staging.ENV.namespace}/{runner_gateway.NAME}" in container["args"]
    assert "--wake-caller=" + staging.ENV.namespace + "/" + app.NAME in container["args"]
    assert "initContainers" not in pod
    assert _resource(docs, "Service", runner_gateway.NAME)["spec"]["ports"] == [
        {"name": "grpc", "port": 8080, "protocol": "TCP", "targetPort": 8080},
        {"name": "runner-control", "port": 8082, "protocol": "TCP", "targetPort": 8082},
    ]
    assert _resource(docs, "Service", session_dispatcher.NAME)["spec"]["ports"] == [
        {"name": "grpc", "port": 8080, "protocol": "TCP", "targetPort": 8081}
    ]
    policy = _resource(docs, "CiliumNetworkPolicy", runner_gateway.NAME.removeprefix("agentplane-"))["spec"]
    ingress = {
        (endpoint["matchLabels"].get("app.kubernetes.io/name"), port["port"])
        for rule in policy["ingress"]
        for endpoint in rule.get("fromEndpoints", [])
        for port in rule.get("toPorts", [{}])[0].get("ports", [])
    }
    assert (app.NAME, "8081") in ingress
    assert (notifications.NAME, "8081") in ingress
    assert (runner_gateway.NAME, "8080") in ingress
    assert (sandbox_controller.NAME, "8080") in ingress
    assert (sandbox_controller.NAME, "8081") not in ingress
    role = _resource(docs, "Role", runner_gateway.NAME.removeprefix("agentplane-"))
    assert {rule["resources"][0] for rule in role["rules"]} == {"pods", "sandboxes", "leases", "endpointslices"}
    assert "patch" in one(rule for rule in role["rules"] if rule["resources"] == ["sandboxes"])["verbs"]


def test_goose_job_name_follows_pinned_image_digest() -> None:
    digest = "a" * 64
    image = f"git.allegedly.works/ducktape-ci/agentplane-runner-gateway-queue-migrate@sha256:{digest}"
    chart = Chart(Cdk8sTesting.app(), "migration", disable_resource_name_hashes=True)
    migration = runner_gateway_go.QueueMigration(chart, "migration", staging.ENV, image=image)
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    job = _resource(docs, "Job", "runner-gateway-queue-migrate-aaaaaaaaaaaa")
    assert migration.name == job["metadata"]["name"]
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["containers"][0]["image"] == image
    assert pod["containers"][0].get("args", []) == []
    assert {item["name"]: item["valueFrom"] for item in pod["containers"][0]["env"]} == {
        "DATABASE_URL": {"secretKeyRef": {"name": "postgres-session-dispatcher", "key": "uri"}}
    }
    with pytest.raises(ValueError, match="full sha256 digest"):
        runner_gateway_go.QueueMigration(
            Chart(Cdk8sTesting.app(), "unpinned"),
            "migration",
            staging.ENV,
            image=image.replace("@sha256:" + digest, ":latest"),
        )


if __name__ == "__main__":
    pytest_bazel.main()
