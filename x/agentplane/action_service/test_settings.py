"""Reviewed testing YAML through real kustomize, mounted Settings, and the MCP binding parser."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import pytest
import pytest_bazel
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from more_itertools import one
from pydantic import JsonValue, TypeAdapter

from util.bazel.runfiles import get_required_path
from x.agentplane.action_service.catalog import ActionCatalog
from x.agentplane.action_service.main import Settings
from x.agentplane.action_service.mcp_executor import (
    McpActionGroupExecutor,
    McpHttpServerConfig,
    McpHttpServerConfigValue,
)
from x.agentplane.action_service.push import PushIdentity
from x.agentplane.action_service.runtime import running_executor


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, Any]]:
    kustomize = get_required_path("multitool/tools/kustomize/kustomize")
    actions = get_required_path("_main/cluster/k8s/agentplane-testing/actions/kustomization.yaml").parent
    return list(yaml.safe_load_all(subprocess.check_output([str(kustomize), "build", str(actions)])))


@pytest.fixture(scope="module")
def staging_rendered() -> list[dict[str, Any]]:
    kustomize = get_required_path("multitool/tools/kustomize/kustomize")
    actions = get_required_path("_main/cluster/k8s/agentplane-staging/actions/kustomization.yaml").parent
    return list(yaml.safe_load_all(subprocess.check_output([str(kustomize), "build", str(actions)])))


@pytest.fixture
def settings(rendered: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    deployment = one(r for r in rendered if r["kind"] == "Deployment" and r["metadata"]["name"] == "agentplane-actions")
    pod = deployment["spec"]["template"]["spec"]
    container = one(c for c in pod["containers"] if c["name"] == "actions")
    path = Path(one(e["value"] for e in container["env"] if e["name"] == "AGENTPLANE_ACTIONS_CONFIG_FILE"))
    mount = one(m for m in container["volumeMounts"] if Path(m["mountPath"]) == path.parent)
    assert mount["readOnly"]
    volume = one(v for v in pod["volumes"] if v["name"] == mount["name"])
    config_map = one(
        r for r in rendered if r["kind"] == "ConfigMap" and r["metadata"]["name"] == volume["configMap"]["name"]
    )
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(config_map["data"][path.name])
    monkeypatch.setenv("AGENTPLANE_ACTIONS_CONFIG_FILE", str(config_file))
    return Settings(database_url="postgresql://test.invalid/test", _cli_parse_args=False)


@pytest.fixture
def staging_settings(
    staging_rendered: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    deployment = one(
        r for r in staging_rendered if r["kind"] == "Deployment" and r["metadata"]["name"] == "agentplane-actions"
    )
    pod = deployment["spec"]["template"]["spec"]
    container = one(c for c in pod["containers"] if c["name"] == "actions")
    path = Path(one(e["value"] for e in container["env"] if e["name"] == "AGENTPLANE_ACTIONS_CONFIG_FILE"))
    mount = one(m for m in container["volumeMounts"] if Path(m["mountPath"]) == path.parent)
    volume = one(v for v in pod["volumes"] if v["name"] == mount["name"])
    config_map = one(
        r for r in staging_rendered if r["kind"] == "ConfigMap" and r["metadata"]["name"] == volume["configMap"]["name"]
    )
    config_file = tmp_path / "settings.yaml"
    config_file.write_text(config_map["data"][path.name])
    private_key = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )
    monkeypatch.setenv("AGENTPLANE_ACTIONS_WEB_PUSH__PRIVATE_KEY_PEM", private_key)
    monkeypatch.setenv("AGENTPLANE_ACTIONS_CONFIG_FILE", str(config_file))
    return Settings(database_url="postgresql://test.invalid/test", _cli_parse_args=False)


@pytest.fixture(scope="module")
def ssh_resources() -> list[dict[str, Any]]:
    kustomize = get_required_path("multitool/tools/kustomize/kustomize")
    root = get_required_path("_main/cluster/k8s/ssh-mcp/kustomization.yaml").parent
    return [
        resource
        for directory in (root, root / "secrets")
        for resource in yaml.safe_load_all(subprocess.check_output([str(kustomize), "build", str(directory)]))
    ]


def test_rendered_ssh_binding_uses_shared_bearer_file(
    staging_settings: Settings, staging_rendered: list[dict[str, Any]], ssh_resources: list[dict[str, Any]]
) -> None:
    group = staging_settings.action_groups["ssh"]
    config: McpHttpServerConfigValue = TypeAdapter(McpHttpServerConfig).validate_python(group.executor.config)
    assert config.auth == "static_bearer"
    McpActionGroupExecutor.from_group("ssh", group)
    assert staging_settings.fixture_auto_allow is None
    deployment = one(r for r in staging_rendered if r["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    actions = one(pod["containers"])
    mount = one(m for m in actions["volumeMounts"] if Path(m["mountPath"]) == config.bearer_file.parent)
    assert mount["readOnly"]
    volume = one(v for v in pod["volumes"] if v["name"] == mount["name"])
    key = one(i for i in volume["secret"]["items"] if i["path"] == config.bearer_file.name)["key"]
    source = one(
        r
        for r in ssh_resources
        if r["kind"] == "ExternalSecret" and "dataFrom" in r["spec"] and "secretStoreRef" not in r["spec"]
    )
    assert volume["secret"]["secretName"] == source["spec"]["target"]["name"]
    assert key in source["spec"]["target"]["template"]["data"]
    assert volume["secret"]["secretName"] in deployment["metadata"]["annotations"][
        "secret.reloader.stakater.com/reload"
    ].split(",")
    backend = one(r for r in ssh_resources if r["kind"] == "Deployment")
    server = one(backend["spec"]["template"]["spec"]["containers"])
    bearer = one(e for e in server["env"] if e["name"] == "SSH_MCP_BEARER_TOKEN")["valueFrom"]["secretKeyRef"]
    assert bearer == {"name": volume["secret"]["secretName"], "key": key}
    service = one(r for r in ssh_resources if r["kind"] == "Service")
    endpoint = urlsplit(str(config.url))
    assert endpoint.hostname == f"{service['metadata']['name']}.{service['metadata']['namespace']}.svc.cluster.local"
    assert endpoint.port == one(service["spec"]["ports"])["port"]
    assert endpoint.path == "/mcp"
    assert service["spec"]["selector"].items() <= backend["spec"]["template"]["metadata"]["labels"].items()
    # Private SSH material never crosses the backend namespace boundary.
    key_volume = one(v for v in backend["spec"]["template"]["spec"]["volumes"] if v["name"] == "keys")
    assert all(v.get("secret", {}).get("secretName") != key_volume["secret"]["secretName"] for v in pod["volumes"])


def test_bearer_is_generated_once_and_shared_only_with_approved_consumers(ssh_resources: list[dict[str, Any]]) -> None:
    password = one(r for r in ssh_resources if r["kind"] == "Password")
    source = one(r for r in ssh_resources if r["kind"] == "ExternalSecret" and "secretStoreRef" not in r["spec"])
    assert source["metadata"]["namespace"] == password["metadata"]["namespace"] == "ssh-mcp"
    assert source["spec"]["refreshPolicy"] == "CreatedOnce"
    assert source["spec"]["target"]["creationPolicy"] == "Owner"
    assert one(source["spec"]["dataFrom"])["sourceRef"]["generatorRef"] == {
        "apiVersion": password["apiVersion"],
        "kind": password["kind"],
        "name": password["metadata"]["name"],
    }
    template = source["spec"]["target"]["template"]
    assert template["data"] == {"bearer-token": "{{ .password }}"}
    annotations = template["metadata"]["annotations"]
    for mode in ("allowed", "auto"):
        assert set(annotations[f"reflector.v1.k8s.emberstack.com/reflection-{mode}-namespaces"].split(",")) == {
            "^haku-console$",
            "^agentplane-staging$",
        }
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-allowed"] == "true"
    assert annotations["reflector.v1.k8s.emberstack.com/reflection-auto-enabled"] == "true"
    policy = one(r for r in ssh_resources if r["kind"] == "CiliumNetworkPolicy")
    ingress = one(policy["spec"]["ingress"])
    assert {rule["matchLabels"]["k8s:io.kubernetes.pod.namespace"] for rule in ingress["fromEndpoints"]} == {
        "haku-console",
        "agentplane-staging",
    }


def test_testing_has_no_ssh_binding_or_credentials(settings: Settings, rendered: list[dict[str, Any]]) -> None:
    assert "ssh" not in settings.action_groups
    assert not any(r["metadata"]["name"].startswith("ssh-mcp") for r in rendered)
    for deployment in (r for r in rendered if r["kind"] == "Deployment"):
        pod = deployment["spec"]["template"]["spec"]
        assert all(not v.get("secret", {}).get("secretName", "").startswith("ssh-mcp") for v in pod.get("volumes", []))


def test_rendered_remote_binding_reaches_existing_service(settings: Settings, rendered: list[dict[str, Any]]) -> None:
    assert settings.fixture_auto_allow is not None
    group = settings.action_groups[settings.fixture_auto_allow.group]
    config: McpHttpServerConfigValue = TypeAdapter(McpHttpServerConfig).validate_python(group.executor.config)
    endpoint = urlsplit(str(config.url))
    service = one(
        r
        for r in rendered
        if r["kind"] == "Service"
        and endpoint.hostname == f"{r['metadata']['name']}.{r['metadata']['namespace']}.svc.cluster.local"
    )
    deployment = one(
        r
        for r in rendered
        if r["kind"] == "Deployment"
        and service["spec"]["selector"].items() <= r["spec"]["template"]["metadata"]["labels"].items()
    )
    pod = deployment["spec"]["template"]["spec"]
    port = one(p for p in service["spec"]["ports"] if p["port"] == endpoint.port)
    container = one(c for c in pod["containers"] if any(p["name"] == port["targetPort"] for p in c["ports"]))
    assert one(p["containerPort"] for p in container["ports"] if p["name"] == port["targetPort"]) == endpoint.port
    # The existing image's streamableHttp entrypoint serves /mcp, not the legacy SSE endpoint.
    assert container["command"][-1] == "streamableHttp"
    assert endpoint.path == "/mcp"
    assert service["metadata"]["namespace"] in settings.allowed_service_account_namespaces
    assert pod["automountServiceAccountToken"] is False
    assert not group.actions
    catalog = ActionCatalog(groups=settings.action_groups)
    assert len(settings.decision_providers(catalog)) == 1
    McpActionGroupExecutor.from_group(settings.fixture_auto_allow.group, group)


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"transport": "sse", "url": "https://test.invalid/mcp"},
        {
            "transport": "streamable-http",
            "url": "https://test.invalid/mcp",
            "headers": {"Authorization": "test-only-private"},
        },
        {"transport": "streamable-http", "url": "https://test.invalid/mcp?token=test-only-private"},
    ],
)
async def test_reviewed_binding_errors_fail_before_any_connection(
    settings: Settings, config: dict[str, JsonValue]
) -> None:
    assert settings.fixture_auto_allow is not None
    settings.action_groups[settings.fixture_auto_allow.group].executor.config = config
    with patch.object(McpActionGroupExecutor, "start", new_callable=AsyncMock) as start:
        with pytest.raises(ValueError, match="invalid MCP binding") as error:
            async with running_executor(ActionCatalog(groups=settings.action_groups)):
                pytest.fail("malformed reviewed binding was served")
        assert "test-only-private" not in str(error.value)
        start.assert_not_awaited()


def test_staging_push_key_config_and_egress_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    root = get_required_path("_main/cluster/k8s/agentplane-staging/actions/kustomization.yaml").parent
    kustomize = get_required_path("multitool/tools/kustomize/kustomize")
    resources = list(yaml.safe_load_all(subprocess.check_output([str(kustomize), "build", str(root)])))
    deployment = one(r for r in resources if r["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    container = one(c for c in pod["containers"] if c["name"] == "actions")
    key_env = one(e for e in container["env"] if e["name"] == "AGENTPLANE_ACTIONS_WEB_PUSH__PRIVATE_KEY_PEM")
    reference = key_env["valueFrom"]["secretKeyRef"]
    secret = one(r for r in resources if r["kind"] == "Secret" and r["metadata"]["name"] == reference["name"])
    assert secret["metadata"]["namespace"] == "agentplane-staging"
    assert secret["stringData"][reference["key"]].startswith("ENC[AES256_GCM,")
    assert reference["name"] in deployment["metadata"]["annotations"]["secret.reloader.stakater.com/reload"].split(",")
    flux = yaml.safe_load((root / "flux-kustomization.yaml").read_text())
    assert flux["spec"]["decryption"] == {"provider": "sops", "secretRef": {"name": "sops-age-cluster-secrets"}}

    private_key = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )
    monkeypatch.setenv(key_env["name"], private_key)
    monkeypatch.setenv("AGENTPLANE_ACTIONS_CONFIG_FILE", str(root / "settings.yaml"))
    settings = Settings(database_url="postgresql://test.invalid/test", _cli_parse_args=False)
    assert settings.web_push is not None
    assert settings.web_push.public_base_url == "https://agentplane-staging.allegedly.works"
    identity = PushIdentity(settings.web_push)
    assert identity.application_server_key == PushIdentity(settings.web_push).application_server_key
    assert len(identity.application_server_key) == 87
    for host in settings.web_push.allowed_push_hosts:
        identity.validate_endpoint(f"https://{host}/test-subscription")
        assert identity.authorization(f"https://{host}/test-subscription").startswith("vapid ")
    for endpoint in (
        "https://unreviewed.example/push",
        "http://fcm.googleapis.com/push",
        "https://fcm.googleapis.com:8443/push",
    ):
        with pytest.raises(ValueError, match="configured HTTPS push service"):
            identity.validate_endpoint(endpoint)

    policy = one(r for r in resources if r["kind"] == "CiliumNetworkPolicy")
    rule = one(
        r
        for r in policy["spec"]["egress"]
        if any(host.get("matchName") in settings.web_push.allowed_push_hosts for host in r.get("toFQDNs", []))
    )
    assert {r["matchName"] for r in rule["toFQDNs"]} == settings.web_push.allowed_push_hosts
    port = one(rule["toPorts"])
    assert port["ports"] == [{"port": "443", "protocol": "TCP"}]
    assert set(port["serverNames"]) == settings.web_push.allowed_push_hosts
    dns = one(r for r in policy["spec"]["egress"] if any("rules" in p for p in r.get("toPorts", [])))
    assert one(dns["toPorts"])["rules"]["dns"] == [{"matchPattern": "*"}]


if __name__ == "__main__":
    pytest_bazel.main()
