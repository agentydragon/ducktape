"""Rewrites the generated `agentplane-testing` manifest into something a bare k3s can run.

The target is a guest kernel with no Cilium, no kube-proxy, no Gateway API, no KubeVirt and none
of the operators the cluster provides (cert-manager, trust-manager, External Secrets, CNPG,
Kyverno, the Prometheus operator). Objects of those kinds are dropped or replaced by plain ones.
"""

import base64
import copy
import re
import secrets
import string
from collections.abc import Iterable, Mapping
from typing import Any

NAMESPACE = "agentplane-testing"
POSTGRES_SERVICE = "postgres-rw"

# Kinds a bare k3s has no CRD for and that the services do not read.
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
        "Cluster",
        "Database",
    }
)
# The VM template needs KubeVirt.
DROPPED_NAMES = frozenset(
    {
        ("SandboxTemplate", "runner-vm"),
        # Its config template calls `regexReplaceAll`, which the image's gomplate does not define.
        ("Deployment", "dex"),
        ("Service", "dex"),
    }
)


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


def _pod_spec_fixups(pod: dict[str, Any], node_ip: str) -> None:
    pod.pop("imagePullSecrets", None)
    pod.pop("nodeSelector", None)
    pod.pop("tolerations", None)
    pod.pop("affinity", None)
    pod.pop("topologySpreadConstraints", None)
    for container in [*pod.get("containers", []), *pod.get("initContainers", [])]:
        container["imagePullPolicy"] = "IfNotPresent"
        # No kube-proxy means no `kubernetes.default` ClusterIP; in-cluster clients dial the apiserver directly.
        container.setdefault("env", []).extend(
            [
                {"name": "KUBERNETES_SERVICE_HOST", "value": node_ip},
                {"name": "KUBERNETES_SERVICE_PORT", "value": "6443"},
            ]
        )


def rewrite(docs: Iterable[Mapping[str, Any]], *, overrides: Mapping[str, str], node_ip: str) -> list[dict[str, Any]]:
    """The manifest `docs` rewritten for k3s; `overrides` maps a Secret name to the one value it holds."""
    out: list[dict[str, Any]] = []
    for original in docs:
        doc = copy.deepcopy(dict(original))
        kind, name = doc["kind"], doc["metadata"]["name"]
        if kind in DROPPED_KINDS or (kind, name) in DROPPED_NAMES:
            continue
        if kind == "ExternalSecret":
            doc = _external_secret_to_secret(doc, overrides)
        elif kind == "Service":
            _headless(doc)
        elif kind == "Deployment":
            _pod_spec_fixups(doc["spec"]["template"]["spec"], node_ip)
        elif kind == "SandboxTemplate":
            _pod_spec_fixups(doc["spec"]["pod"]["spec"], node_ip)
        out.append(doc)
    return out


def cnpg_app_secret(password: str) -> dict[str, Any]:
    """The Secret CNPG generates for the `app` owner of its initdb database."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "postgres-app", "namespace": NAMESPACE},
        "stringData": {
            "username": "app",
            "password": password,
            "host": "postgres-rw.agentplane-testing.svc",
            "port": "5432",
            "dbname": "app",
        },
    }


def postgres_objects(roles: Mapping[str, str]) -> list[dict[str, Any]]:
    """A single plain Postgres under the name the services dial, with each role and its database."""
    sql = "\n".join(
        f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"
        + (" REPLICATION" if role == "electric" else "")
        + f";\nCREATE DATABASE {role} OWNER {role};"
        for role, password in roles.items()
    )
    labels = {"app": "postgres"}
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "postgres-init", "namespace": NAMESPACE},
            "data": {"init.sql": sql},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "postgres", "namespace": NAMESPACE},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [
                            {
                                "name": "postgres",
                                "image": "docker.io/library/postgres:18",
                                "args": ["-c", "wal_level=logical", "-c", "max_connections=300", "-c", "fsync=off"],
                                "env": [{"name": "POSTGRES_PASSWORD", "value": _password()}],
                                "ports": [{"containerPort": 5432}],
                                "readinessProbe": {
                                    "exec": {"command": ["pg_isready", "-U", "postgres"]},
                                    "periodSeconds": 2,
                                },
                                "volumeMounts": [{"name": "init", "mountPath": "/docker-entrypoint-initdb.d"}],
                            }
                        ],
                        "volumes": [{"name": "init", "configMap": {"name": "postgres-init"}}],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": POSTGRES_SERVICE, "namespace": NAMESPACE},
            "spec": {"clusterIP": "None", "selector": labels, "ports": [{"port": 5432, "targetPort": 5432}]},
        },
    ]


def secret_value(secret: Mapping[str, Any], key: str) -> str:
    return secret["stringData"][key] if "stringData" in secret else base64.b64decode(secret["data"][key]).decode()


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
