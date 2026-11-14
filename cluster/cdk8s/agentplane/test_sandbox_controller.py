"""The Controller reads static settings from its mounted file and the rotating token separately."""

from pathlib import PurePosixPath
from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from agentplane.net.peertls.config import SANDBOX_CONTROLLER_URI
from agentplane.sandbox_controller.settings import Settings
from cluster.cdk8s.agentplane import environment_sandbox, runner_gateway, sandbox_controller, workload_tls


def _resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


def test_controller_settings_are_mounted_and_gateway_token_is_projected(
    staging_manifests: list[dict[str, Any]],
) -> None:
    config_map = _resource(staging_manifests, "ConfigMap", f"{sandbox_controller.NAME}-settings")
    deployment = _resource(staging_manifests, "Deployment", sandbox_controller.NAME)
    pod = deployment["spec"]["template"]["spec"]
    container = one(pod["containers"])
    env = {entry["name"]: entry for entry in container["env"]}

    config_path = PurePosixPath(env[Settings.config_file_env]["value"])
    assert {name for name in env if name.startswith("AGENTPLANE_SANDBOX_CONTROLLER_")} == {Settings.config_file_env}
    config_volume = one(
        volume for volume in pod["volumes"] if volume.get("configMap", {}).get("name") == config_map["metadata"]["name"]
    )
    config_mount = one(mount for mount in container["volumeMounts"] if mount["name"] == config_volume["name"])
    assert config_mount["mountPath"] == str(config_path.parent)
    assert config_mount["readOnly"] is True
    settings = yaml.safe_load(config_map["data"][config_path.name])
    Settings.model_validate(settings)
    assert settings["runner_security"] == {
        "mode": "mtls",
        "tls": {
            "cert_file": "/run/agentplane/sandbox-controller-client-tls/tls.crt",
            "key_file": "/run/agentplane/sandbox-controller-client-tls/tls.key",
            "ca_file": workload_tls.TRUST_BUNDLE_FILE,
        },
    }

    certificate = _resource(staging_manifests, "Certificate", f"{sandbox_controller.NAME}-client-workload")
    assert certificate["spec"]["uris"] == [SANDBOX_CONTROLLER_URI]
    assert certificate["spec"]["usages"] == ["digital signature", "client auth"]
    cert_mount = one(
        mount
        for mount in container["volumeMounts"]
        if mount["mountPath"] == "/run/agentplane/sandbox-controller-client-tls"
    )
    cert_volume = one(volume for volume in pod["volumes"] if volume["name"] == cert_mount["name"])
    assert cert_mount["readOnly"] is True
    assert cert_volume["secret"]["secretName"] == f"{sandbox_controller.NAME}-client-workload-tls"

    token_path = PurePosixPath(settings["runner_gateway_token_file"])
    token_mount = one(mount for mount in container["volumeMounts"] if mount["mountPath"] == str(token_path.parent))
    token_volume = one(volume for volume in pod["volumes"] if volume["name"] == token_mount["name"])
    assert token_mount["readOnly"] is True
    token_source = one(
        source["serviceAccountToken"]
        for source in token_volume["projected"]["sources"]
        if source.get("serviceAccountToken", {}).get("path") == token_path.name
    )
    assert token_source["audience"] == runner_gateway.TOKEN_AUDIENCE

    controller_policy = _resource(staging_manifests, "CiliumNetworkPolicy", sandbox_controller.NAME)["spec"]
    runtime_label = environment_sandbox.pods("agentplane").selector["app.kubernetes.io/name"]
    assert any(
        endpoint["matchLabels"].get("app.kubernetes.io/name") == runtime_label
        and any(
            port["port"] == str(environment_sandbox.ENVIRONMENT_RUNTIME_SERVICE_PORT)
            for target in rule.get("toPorts", [])
            for port in target["ports"]
        )
        for rule in controller_policy["egress"]
        for endpoint in rule.get("toEndpoints", [])
    )

    runtime_listener = _resource(staging_manifests, "CiliumNetworkPolicy", "environment-tools-listener")["spec"]
    assert any(
        endpoint["matchLabels"].get("app.kubernetes.io/name") == sandbox_controller.NAME
        and any(
            port["port"] == str(environment_sandbox.ENVIRONMENT_RUNTIME_SERVICE_PORT)
            for target in rule.get("toPorts", [])
            for port in target["ports"]
        )
        for rule in runtime_listener["ingress"]
        for endpoint in rule.get("fromEndpoints", [])
    )


if __name__ == "__main__":
    pytest_bazel.main()
