"""Focused contracts over Haku's deployed Kubernetes manifests."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


def test_haku_claude_oauth_proxy_isolated_from_general_sandbox(k8s_dir: Path) -> None:
    """Only the dedicated Haku Claude runner receives proxy authority."""
    template = yaml.safe_load((k8s_dir / "haku/workspaces/app/sandboxtemplate-haku-claude.yaml").read_text())
    template_namespace = template["metadata"]["namespace"]
    console_config = yaml.safe_load((k8s_dir / "haku/console/config.yaml").read_text())
    runtime = console_config["harnesses"]["claude_code"]
    assert runtime["namespace"] == template_namespace == "haku-runtime-sandbox"
    assert runtime["agent_id"] == "8d5b0cba-a9ab-4c93-8c31-70d5c7af45c2"
    assert runtime["claim_prefix"] == "claude"
    assert runtime["harness_label"] == "claude"
    # Claude Code's inference runs against the in-cluster LiteLLM gateway (-> CLIProxyAPI), never
    # api.anthropic.com. Tie the runner's gateway origin and auth placeholder to the fence endpoint.
    assert runtime["api_base_url"] == "http://litellm.litellm.svc.cluster.local:4000"
    assert runtime["oauth_placeholder"] == "proxy-litellm-claude-oauth-placeholder"
    pod_template = template["spec"]["podTemplate"]
    assert pod_template["metadata"]["labels"]["haku.allegedly.works/access-profile-id"] == "haku"
    assert pod_template["spec"]["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in pod_template["spec"]

    mounts = pod_template["spec"]["containers"][0]["volumeMounts"]
    ca_mount = one(mount for mount in mounts if mount["name"] == "egress-proxy-ca")
    assert str(PurePosixPath(runtime["ca_bundle"]).parent) == ca_mount["mountPath"]

    oauth_ingress = yaml.safe_load((k8s_dir / "agents/haku-egress-proxy/claude-networkpolicy.yaml").read_text())
    peers = oauth_ingress["spec"]["ingress"][0]["from"]
    namespace_by_peer = {
        peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]: peer for peer in peers
    }
    assert set(namespace_by_peer) == {template_namespace}
    assert namespace_by_peer[template_namespace]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "haku-harness-runner",
        "haku.allegedly.works/access-profile-id": "haku",
    }

    general_egress = (k8s_dir / "agents/haku-egress-proxy/ccnp-haku-proxy-egress.yaml").read_text()
    assert "haku-claude-oauth-proxy" not in general_egress

    agent_egress_path = k8s_dir / "agents/haku-egress-proxy/ccnp-haku-agent-egress.yaml"
    agent_egress_text = agent_egress_path.read_text()
    assert "haku-claude-oauth-proxy" not in agent_egress_text


def test_public_coder_codex_has_empty_workspace_and_shared_trust_path(k8s_dir: Path) -> None:
    """The Web-launched Codex pair has public-coder placement, prompt and credentials."""
    namespace = "haku-runtime-sandbox"
    template_path = k8s_dir / "haku/workspaces/app/sandboxtemplate-haku-public-coder-codex.yaml"
    template_text = template_path.read_text()
    template = yaml.safe_load(template_text)
    assert template["metadata"]["namespace"] == namespace
    pod = template["spec"]["podTemplate"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in pod
    container = one(pod["containers"])
    assert container["image"].startswith("ghcr.io/agentydragon/haku-harness-runner:devel-")
    assert '# {"$imagepolicy": "flux-system:haku-harness-runner"}' in template_text
    assert container["args"] == ["--harness", "codex-app-server"]
    environment = sandbox_env(template)
    assert environment["HAKU_RUNNER_WEBSOCKET_URL"]["value"] == (
        "ws://haku-console.haku-console.svc.cluster.local:9090/internal/claude/runner"
    )
    # Delete with the template's CLEANUP: the legacy spelling rides along, same value, while
    # runner images that predate the rename may still be pinned.
    assert (
        environment["HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL"]["value"]
        == (environment["HAKU_RUNNER_WEBSOCKET_URL"]["value"])
    )
    assert environment["OPENAI_API_KEY"] == {
        "name": "OPENAI_API_KEY",
        "value": "proxy-litellm-public-coder-placeholder",
    }
    assert environment["HAKU_RUNNER_SETUP"]["value"] == ""
    assert environment["GIT_CONFIG_COUNT"]["value"] == "2"
    assert environment["GIT_CONFIG_KEY_0"]["value"] == "user.name"
    assert environment["GIT_CONFIG_VALUE_0"]["value"] == "public-coder-agent"
    assert environment["GIT_CONFIG_KEY_1"]["value"] == "user.email"
    assert environment["GIT_CONFIG_VALUE_1"]["value"] == "public-coder-agent@allegedly.works"
    assert not {"HAKU_GIT_USERNAME", "HAKU_GIT_PASSWORD"} & environment.keys()
    workspace = one(volume for volume in pod["volumes"] if volume["name"] == "workspace")
    assert workspace == {"name": "workspace", "emptyDir": {"sizeLimit": "10Gi"}}
    # The runner trusts the colocated Console egress fence it now routes through (#4670): the bundle
    # mounted at the system trust path is the fence CA (haku-egress-proxy-ca-cert), replacing the
    # former dedicated runner proxy's, so GnuTLS git and everything else verify the fence's leaves.
    trust_mount = one(mount for mount in container["volumeMounts"] if mount["name"] == "egress-proxy-ca")
    assert trust_mount["mountPath"] == "/etc/ssl/certs/ca-certificates.crt"
    assert trust_mount["subPath"] == "ca-certificates.crt"
    trust_volume = one(volume for volume in pod["volumes"] if volume["name"] == "egress-proxy-ca")
    assert trust_volume["configMap"]["name"] == "haku-egress-proxy-ca-cert"

    policy_objects = list(yaml.safe_load_all((k8s_dir / "haku/runtime-namespace/networkpolicy.yaml").read_text()))
    egress = one(obj for obj in policy_objects if obj["metadata"]["name"] == "public-coder-runner-egress")
    destinations = {
        (
            one(rule["toEndpoints"])["matchLabels"]["k8s:io.kubernetes.pod.namespace"],
            one(rule["toEndpoints"])["matchLabels"].get("k8s:app.kubernetes.io/name"),
            int(one(one(rule["toPorts"])["ports"])["port"]),
        )
        for rule in egress["spec"]["egress"]
        if "toEndpoints" in rule and len(one(rule["toPorts"])["ports"]) == 1
    }
    assert destinations == {
        ("haku-console", "haku-egress-proxy", 8888),
        ("haku-console", "haku-console", 9090),
        ("haku-runtime-sandbox", None, 53),
        ("haku-runtime-sandbox", None, 443),
        ("haku-runtime-sandbox", None, 80),
    }
