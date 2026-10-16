"""Rewrites the generated `agentplane-testing` manifest into something a bare k3s can run.

The target is a guest kernel with no Cilium, no kube-proxy, no Gateway API, no KubeVirt and none
of the operators the cluster provides (cert-manager, trust-manager, External Secrets, CNPG,
Kyverno, the Prometheus operator). Objects of those kinds are dropped or replaced by plain ones.
"""

import copy
import re
import secrets
import string
from collections.abc import Iterable, Mapping
from typing import Any

NAMESPACE = "agentplane-testing"
POSTGRES_SERVICE = "postgres-rw"
RUNNER_GATEWAY = "runner-gateway"
SESSION_DISPATCHER = "session-dispatcher"
GO_RUNNER_GATEWAY_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-gateway-go:unset"
GO_QUEUE_MIGRATOR_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner-gateway-queue-migrate:unset"
DISPATCHER_PORT = 8080

# Kinds a bare k3s has no CRD for and that the services do not read.
LITELLM_INTERNAL_HOST = "litellm.litellm.svc.cluster.local"
LITELLM_EXTERNAL_HOST = "litellm.allegedly.works"

DROPPED_KINDS = frozenset(
    {
        "CiliumNetworkPolicy",
        "HTTPRoute",
        "ClusterPolicy",
        "PrometheusRule",
        "ServiceMonitor",
        "Certificate",
        "Bundle",
        "ClusterSecretStore",
        "Password",
        "ResourceQuota",
        "LimitRange",
    }
)
# The VM template needs KubeVirt.
DROPPED_NAMES = frozenset(
    {
        ("SandboxTemplate", "runner-vm"),
        # Its config template calls `regexReplaceAll`, which the image's gomplate does not define.
        ("Deployment", "dex"),
        ("Service", "dex"),
        # The registry credentials are not available, and the image pull secrets are dropped anyway.
        ("ExternalSecret", "forgejo-images-creds"),
    }
)


PRIMARY_SERVICE = "postgres-primary"


def extra_namespaces() -> list[dict[str, Any]]:
    """Namespaces the manifest's Roles live in, for the cluster's LiteLLM and Ollama."""
    return [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}} for name in ("litellm", "ollama")]


def kubevirt_stub_crds() -> list[dict[str, Any]]:
    """Schemaless `VirtualMachine`/`VirtualMachineInstance` kinds: the sandbox controller's cache needs them to exist."""
    return [
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": f"{plural}.kubevirt.io"},
            "spec": {
                "group": "kubevirt.io",
                "scope": "Namespaced",
                "names": {"kind": kind, "plural": plural, "singular": kind.lower()},
                "versions": [
                    {
                        "name": "v1",
                        "served": True,
                        "storage": True,
                        "schema": {"openAPIV3Schema": {"type": "object", "x-kubernetes-preserve-unknown-fields": True}},
                    }
                ],
            },
        }
        for kind, plural in (
            ("VirtualMachine", "virtualmachines"),
            ("VirtualMachineInstance", "virtualmachineinstances"),
        )
    ]


def _password(length: int = 24) -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))


def _render_template(text: str, values: Mapping[str, str]) -> str:
    """Go-template subset External Secrets templates use here: `{{ .key }}` and `{{ index . "key" }}`."""
    text = re.sub(r"\{\{\s*\.(\w+)\s*\}\}", lambda m: values[m.group(1)], text)
    return re.sub(r'\{\{\s*index \. "([\w-]+)"\s*\}\}', lambda m: values[m.group(1)], text)


def _external_secret_to_secret(doc: Mapping[str, Any], overrides: Mapping[str, str]) -> dict[str, Any]:
    spec = doc["spec"]
    target = spec.get("target", {})
    name = target.get("name", doc["metadata"]["name"])
    values: dict[str, str] = {}
    for source in spec.get("dataFrom", []):
        key = "password"
        for rewrite in source.get("rewrite", []):
            key = rewrite["regexp"]["target"]
        values[key] = _password()
    for entry in spec.get("data", []):
        values[entry["secretKey"]] = overrides.get(name, f"dummy-{name}-{entry['secretKey']}")
    template = target.get("template", {})
    data = {k: _render_template(v, values) for k, v in template.get("data", {}).items()} or values
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": doc["metadata"]["namespace"]},
        "type": template.get("type", "Opaque"),
        "stringData": data,
    }


def _headless(service: dict[str, Any]) -> None:
    """No kube-proxy: a ClusterIP routes nowhere, a headless Service resolves to the Pod IPs."""
    spec = service["spec"]
    spec["clusterIP"] = "None"
    spec.pop("type", None)
    for port in spec["ports"]:
        # Clients dial the Service port, which a headless Service does not translate.
        port["port"] = port["targetPort"] if isinstance(port.get("targetPort"), int) else port["port"]


def _cnpg_cluster_fixups(cluster: dict[str, Any], node_ip: str) -> None:
    """The one-node Cluster on k3s: no placement, no PodMonitor, a headless `postgres-rw`, the apiserver by node IP."""
    spec = cluster["spec"]
    spec.pop("affinity", None)
    spec["monitoring"] = {"enablePodMonitor": False}
    spec.get("resources", {}).pop("limits", None)
    spec["storage"]["storageClass"] = "local-path"
    spec["env"] = [
        {"name": "KUBERNETES_SERVICE_HOST", "value": node_ip},
        {"name": "KUBERNETES_SERVICE_PORT", "value": "6443"},
    ]
    # CNPG's own `postgres-rw` is a ClusterIP Service and may not be disabled or replaced; CoreDNS
    # rewrites its name to this headless one.
    spec["managed"]["services"] = {
        "additional": [
            {
                "selectorType": "rw",
                "updateStrategy": "patch",
                "serviceTemplate": {"metadata": {"name": PRIMARY_SERVICE}, "spec": {"clusterIP": "None"}},
            }
        ]
    }


def _use_local_path(node: Any) -> None:
    """The cluster's storage classes do not exist on k3s, which ships `local-path`."""
    if isinstance(node, dict):
        if "storageClassName" in node:
            node["storageClassName"] = "local-path"
        for value in node.values():
            _use_local_path(value)
    elif isinstance(node, list):
        for value in node:
            _use_local_path(value)


def _pod_spec_fixups(pod: dict[str, Any], node_ip: str) -> None:
    pod.pop("imagePullSecrets", None)
    pod.pop("nodeSelector", None)
    pod.pop("tolerations", None)
    pod.pop("affinity", None)
    pod.pop("topologySpreadConstraints", None)
    for container in [*pod.get("containers", []), *pod.get("initContainers", [])]:
        container["imagePullPolicy"] = "IfNotPresent"
        # A CPU limit throttles Python imports by seconds on the one shared VM; requests still place the Pods.
        container.get("resources", {}).pop("limits", None)
        # No kube-proxy means no `kubernetes.default` ClusterIP; in-cluster clients dial the apiserver directly.
        container.setdefault("env", []).extend(
            [
                {"name": "KUBERNETES_SERVICE_HOST", "value": node_ip},
                {"name": "KUBERNETES_SERVICE_PORT", "value": "6443"},
            ]
        )


def _go_runner_gateway(deployment: dict[str, Any]) -> None:
    """Replace the fixture's two Python workloads with the combined Go runtime."""
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    container.update(
        {
            "name": RUNNER_GATEWAY,
            "image": GO_RUNNER_GATEWAY_IMAGE,
            "args": [
                f"--sandbox-namespace={NAMESPACE}",
                "--runner-token-file=/var/run/secrets/agentplane-runner-gateway/token",
                "--dispatcher-audience=session-dispatcher",
                "--gateway-audience=agentplane-runner-gateway",
                "--runner-audience=agentplane-runner-control",
                f"--sandbox-controller-account={NAMESPACE}/sandbox-controller",
                f"--dispatcher-caller={NAMESPACE}/app",
                f"--dispatcher-caller={NAMESPACE}/notifications",
                f"--gateway-caller={NAMESPACE}/app",
                f"--gateway-caller={NAMESPACE}/notifications",
                f"--gateway-caller={NAMESPACE}/{RUNNER_GATEWAY}",
                f"--wake-caller={NAMESPACE}/app",
                f"--wake-caller={NAMESPACE}/notifications",
            ],
            "ports": [{"name": "grpc", "containerPort": 8080, "protocol": "TCP"}],
        }
    )
    env = [entry for entry in container.get("env", []) if entry.get("name") != "AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE"]
    env.append(
        {
            "name": "AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL",
            "valueFrom": {"secretKeyRef": {"key": "uri", "name": "postgres-session-dispatcher"}},
        }
    )
    container["env"] = env
    container["volumeMounts"] = [
        mount
        for mount in container.get("volumeMounts", [])
        if mount.get("mountPath") != "/etc/agentplane-runner-gateway"
    ]

    pod["volumes"] = [
        volume for volume in pod.get("volumes", []) if volume.get("name") != "configmap-runner-gateway-config"
    ]
    pod["volumes"].append(
        {
            "name": "runner-gateway-token",
            "projected": {
                "sources": [
                    {
                        "serviceAccountToken": {
                            "audience": "agentplane-runner-gateway",
                            "expirationSeconds": 3600,
                            "path": "token",
                        }
                    }
                ]
            },
        }
    )
    container["volumeMounts"].append(
        {"mountPath": "/var/run/secrets/agentplane-runner-gateway", "name": "runner-gateway-token", "readOnly": True}
    )
    pod["initContainers"] = [
        {
            "name": "goose-migrate",
            "image": GO_QUEUE_MIGRATOR_IMAGE,
            "imagePullPolicy": "IfNotPresent",
            "env": [
                {
                    "name": "DATABASE_URL",
                    "valueFrom": {"secretKeyRef": {"key": "uri", "name": "postgres-session-dispatcher"}},
                }
            ],
            "resources": {"requests": {"cpu": "25m", "memory": "64Mi"}},
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "privileged": False,
                "readOnlyRootFilesystem": False,
                "runAsNonRoot": True,
            },
        }
    ]


def _go_dispatcher_service(service: dict[str, Any], gateway_selector: Mapping[str, str]) -> None:
    """Keep the stable Service name; headless fixture clients dial the Go Pod port directly."""
    service["spec"]["selector"] = dict(gateway_selector)
    found = False
    for port in service["spec"]["ports"]:
        if port["name"] == "grpc":
            port["targetPort"] = DISPATCHER_PORT
            found = True
    if not found:
        raise ValueError(f"{SESSION_DISPATCHER} Service has no grpc port")


def _go_dispatcher_endpoint(node: Any) -> Any:
    """Without kube-proxy, clients use the Go Pod port behind the stable headless Service name."""
    old = f"{SESSION_DISPATCHER}.{NAMESPACE}.svc.cluster.local:8080"
    new = f"{SESSION_DISPATCHER}.{NAMESPACE}.svc.cluster.local:{DISPATCHER_PORT}"
    if isinstance(node, dict):
        return {key: _go_dispatcher_endpoint(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_go_dispatcher_endpoint(value) for value in node]
    if isinstance(node, str):
        return node.replace(old, new).replace(
            f"{RUNNER_GATEWAY}.{NAMESPACE}.svc.cluster.local:8082",
            f"{RUNNER_GATEWAY}.{NAMESPACE}.svc.cluster.local:8080",
        )
    return node


def _use_external_litellm(node: Any) -> Any:
    """Point at the public LiteLLM: the cluster-internal Service is not here, and its rules were `clusterInternal`."""
    if isinstance(node, dict):
        if node.get("hosts") == [LITELLM_INTERNAL_HOST]:
            node["hosts"] = [LITELLM_EXTERNAL_HOST]
            node.pop("clusterInternal", None)
        for key, value in node.items():
            node[key] = _use_external_litellm(value)
        return node
    if isinstance(node, list):
        return [_use_external_litellm(value) for value in node]
    if isinstance(node, str):
        return node.replace(f"http://{LITELLM_INTERNAL_HOST}:4000", f"https://{LITELLM_EXTERNAL_HOST}")
    return node


def rewrite(
    docs: Iterable[Mapping[str, Any]], *, overrides: Mapping[str, str], node_ip: str, external_litellm: bool = False
) -> list[dict[str, Any]]:
    """The manifest `docs` rewritten for k3s.

    `overrides` maps a Secret name to the one value it holds; `external_litellm` sends LiteLLM traffic to the
    public endpoint, which `overrides` must then hold a key for.
    """
    source_docs = [copy.deepcopy(dict(original)) for original in docs]
    gateway = next(
        doc for doc in source_docs if doc["kind"] == "Deployment" and doc["metadata"]["name"] == RUNNER_GATEWAY
    )
    if not any(doc["kind"] == "Deployment" and doc["metadata"]["name"] == SESSION_DISPATCHER for doc in source_docs):
        raise ValueError(f"generated fixture is missing the {SESSION_DISPATCHER} Deployment")
    gateway_selector = gateway["spec"]["selector"]["matchLabels"]
    out: list[dict[str, Any]] = []
    for original in source_docs:
        doc = original
        kind, name = doc["kind"], doc["metadata"]["name"]
        if kind in DROPPED_KINDS or (kind, name) in DROPPED_NAMES:
            continue
        if kind == "Deployment" and name == SESSION_DISPATCHER:
            continue
        if kind == "ExternalSecret":
            doc = _external_secret_to_secret(doc, overrides)
        elif kind == "Service":
            if name == SESSION_DISPATCHER:
                _go_dispatcher_service(doc, gateway_selector)
            if name == RUNNER_GATEWAY:
                doc["spec"]["ports"] = [port for port in doc["spec"]["ports"] if port["name"] == "grpc"]
            _headless(doc)
        elif kind == "Cluster":
            _cnpg_cluster_fixups(doc, node_ip)
        elif kind == "Deployment":
            if name == RUNNER_GATEWAY:
                _go_runner_gateway(doc)
            _pod_spec_fixups(doc["spec"]["template"]["spec"], node_ip)
        elif kind == "Role" and name == RUNNER_GATEWAY:
            for rule in doc.get("rules", []):
                if (
                    "sandboxes" in rule.get("resources", [])
                    and "agentplane.allegedly.works" in rule.get("apiGroups", [])
                    and "patch" not in rule["verbs"]
                ):
                    rule["verbs"].append("patch")
        elif kind == "SandboxTemplate":
            _pod_spec_fixups(doc["spec"]["pod"]["spec"], node_ip)
        _use_local_path(doc)
        doc = _go_dispatcher_endpoint(doc)
        if external_litellm:
            doc = _use_external_litellm(doc)
        out.append(doc)
    return out


def egress_ca_objects(*, ca_cert: str, ca_key: str, system_roots: str, cluster_ca: str) -> list[dict[str, Any]]:
    """What cert-manager and trust-manager produce for the egress proxy: the interception CA's
    Secret and the two trust bundles."""

    def config_map(name: str, roots: str) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": NAMESPACE},
            "data": {"ca-certificates.crt": roots},
        }

    return [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "agentplane-testing-egress-ca", "namespace": NAMESPACE},
            "type": "kubernetes.io/tls",
            "stringData": {"tls.crt": ca_cert, "tls.key": ca_key, "ca.crt": ca_cert},
        },
        config_map("agentplane-testing-egress-ca", system_roots + ca_cert),
        config_map("agentplane-testing-egress-upstream-ca", system_roots + cluster_ca),
    ]
