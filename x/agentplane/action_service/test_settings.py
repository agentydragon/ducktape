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
from pydantic import JsonValue

from util.bazel.runfiles import get_required_path
from x.agentplane.action_service.catalog import ActionCatalog
from x.agentplane.action_service.main import Settings
from x.agentplane.action_service.mcp_executor import McpActionGroupExecutor, McpHttpServerConfig
from x.agentplane.action_service.push import PushIdentity
from x.agentplane.action_service.runtime import running_executor


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, Any]]:
    kustomize = get_required_path("multitool/tools/kustomize/kustomize")
    root = get_required_path("_main/cluster/k8s/agentplane-testing/actions/kustomization.yaml").parent.parent
    return [
        document
        for directory in ("actions", "mcp-everything")
        for document in yaml.safe_load_all(subprocess.check_output([str(kustomize), "build", str(root / directory)]))
    ]


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


def test_rendered_remote_binding_reaches_existing_service(settings: Settings, rendered: list[dict[str, Any]]) -> None:
    assert settings.fixture_auto_allow is not None
    group = settings.action_groups[settings.fixture_auto_allow.group]
    config = McpHttpServerConfig.model_validate(group.executor.config)
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
    rule = one(r for r in policy["spec"]["egress"] if "toFQDNs" in r)
    assert {r["matchName"] for r in rule["toFQDNs"]} == settings.web_push.allowed_push_hosts
    port = one(rule["toPorts"])
    assert port["ports"] == [{"port": "443", "protocol": "TCP"}]
    assert set(port["serverNames"]) == settings.web_push.allowed_push_hosts
    dns = one(r for r in policy["spec"]["egress"] if any("rules" in p for p in r.get("toPorts", [])))
    assert one(dns["toPorts"])["rules"]["dns"] == [{"matchPattern": "*"}]


if __name__ == "__main__":
    pytest_bazel.main()
