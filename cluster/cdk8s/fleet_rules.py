"""Rules every object this repo generates must hold, checked over a chart's rendered
objects when it synthesizes (`App.synth`, `Chart.to_json`, `cdk8s.Testing.synth` all
validate the construct tree first). A violation fails synth with the object named,
instead of an admission denial or a CreateContainerConfigError on the cluster.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import jsii
from cdk8s import ApiObject, Chart
from constructs import IValidation

_POD_TEMPLATE_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet"})
_OUTSIDE_ENTITIES = frozenset({"world", "remote-node", "host"})
_HTTPS_PORT = "443"
# Pod-spec keys that name a Secret or ConfigMap: env valueFrom, envFrom, volumes (plain,
# projected sources), imagePullSecrets.
_SECRET_REF_KEYS = frozenset({"secretKeyRef", "secretRef", "imagePullSecrets"})
_CONFIG_MAP_REF_KEYS = frozenset({"configMapKeyRef", "configMapRef"})


def _pod_specs(objects: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    for obj in objects:
        kind, name = obj["kind"], f"{obj['kind']}/{obj['metadata']['name']}"
        if kind in _POD_TEMPLATE_KINDS:
            yield name, obj["spec"]["template"]["spec"]
        elif kind == "CronJob":
            yield name, obj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        elif kind == "SandboxTemplate":
            yield name, obj["spec"]["podTemplate"]["spec"]


def pod_hardening(objects: list[dict[str, Any]]) -> list[str]:
    """Every container runs under the RuntimeDefault seccomp profile (its own or the
    Pod's) and declares cpu/memory requests and a memory limit. CPU limits are left to
    the namespace LimitRange's default on purpose."""
    errors: list[str] = []
    for name, pod in _pod_specs(objects):
        pod_seccomp = pod.get("securityContext", {}).get("seccompProfile", {}).get("type")
        for container in [*pod.get("initContainers", []), *pod["containers"]]:
            own = f"{name} container {container['name']}"
            seccomp = (container.get("securityContext") or {}).get("seccompProfile", {}).get("type") or pod_seccomp
            if seccomp != "RuntimeDefault":
                errors.append(f"{own}: seccomp profile is {seccomp!r}, not RuntimeDefault")
            resources = container.get("resources", {})
            errors.extend(
                f"{own}: resources.{section} lacks {sorted(missing)}"
                for section, required in (("requests", {"cpu", "memory"}), ("limits", {"memory"}))
                if (missing := required - set(resources.get(section, {})))
            )
    return errors


def pinned_https_egress(objects: list[dict[str, Any]], *, unpinned: frozenset[str]) -> list[str]:
    """A CiliumNetworkPolicy egress rule reaching port 443 outside the cluster (world,
    remote-node, host, FQDNs) names the TLS server names it allows: without SNI it is
    "any HTTPS server". `unpinned` names the policies allowed through by design -- a
    TLS-terminating proxy whose allowlist is its own configuration."""
    errors: list[str] = []
    for obj in objects:
        if obj["kind"] != "CiliumNetworkPolicy" or obj["metadata"]["name"] in unpinned:
            continue
        for rule in obj["spec"].get("egress", []):
            destination = rule.get("toFQDNs") or sorted(_OUTSIDE_ENTITIES & set(rule.get("toEntities", [])))
            if not destination:
                continue
            errors.extend(
                f"CiliumNetworkPolicy/{obj['metadata']['name']}: HTTPS egress to {destination} without serverNames"
                for to_ports in rule.get("toPorts", [])
                if any(port["port"] == _HTTPS_PORT for port in to_ports.get("ports", []))
                and not to_ports.get("serverNames")
            )
    return errors


def _references(pod: dict[str, Any]) -> Iterator[tuple[str, str]]:
    if isinstance(pod, dict):
        for key, value in pod.items():
            if key in _SECRET_REF_KEYS or key in _CONFIG_MAP_REF_KEYS:
                kind = "Secret" if key in _SECRET_REF_KEYS else "ConfigMap"
                for ref in value if isinstance(value, list) else [value]:
                    yield kind, ref["name"]
            elif key == "secretName":
                yield "Secret", value
            elif key in ("secret", "configMap") and isinstance(value, dict) and "name" in value:
                yield ("Secret" if key == "secret" else "ConfigMap"), value["name"]
            yield from _references(value)
    elif isinstance(pod, list):
        for item in pod:
            yield from _references(item)


def resolved_references(objects: list[dict[str, Any]]) -> list[str]:
    """Resolve Pod references against resources synthesized in this chart.

    Controllers can create resources named by an ExternalSecret, Certificate,
    trust-manager Bundle, or CNPG Cluster. References not created in this chart may
    come from sibling files or other Kustomizations and are outside this rule's scope.
    A same-name resource of the other kind is still a local resolution error.
    """
    secrets = {
        *(
            o["spec"].get("target", {}).get("name", o["metadata"]["name"])
            for o in objects
            if o["kind"] == "ExternalSecret"
        ),
        *(o["metadata"]["name"] for o in objects if o["kind"] == "Secret"),
        *(o["spec"]["secretName"] for o in objects if o["kind"] == "Certificate"),
        *(
            f"{o['metadata']['name']}-app"
            for o in objects
            if o["kind"] == "Cluster" and o["apiVersion"].startswith("postgresql.cnpg.io/")
        ),
    }
    config_maps = {o["metadata"]["name"] for o in objects if o["kind"] in ("ConfigMap", "Bundle")}
    created = {"Secret": secrets, "ConfigMap": config_maps}
    other_kind = {"Secret": "ConfigMap", "ConfigMap": "Secret"}
    errors: list[str] = []
    for owner, pod in _pod_specs(objects):
        for kind, name in _references(pod):
            if name in created[kind] or name not in created[other_kind[kind]]:
                continue
            errors.append(f"{owner} reads {kind} {name!r}, but the chart creates a {other_kind[kind]} with that name")
    return errors


@jsii.implements(IValidation)
class FleetRules:
    def __init__(self, chart: Chart, *, unpinned_https_egress: frozenset[str] = frozenset()) -> None:
        self._chart = chart
        self._unpinned_https_egress = unpinned_https_egress

    def validate(self) -> list[str]:
        objects = [
            cast(ApiObject, node).to_json() for node in self._chart.node.find_all() if ApiObject.is_api_object(node)
        ]
        return [
            *pod_hardening(objects),
            *pinned_https_egress(objects, unpinned=self._unpinned_https_egress),
            *resolved_references(objects),
        ]


def add_fleet_rules(chart: Chart, *, unpinned_https_egress: frozenset[str] = frozenset()) -> None:
    chart.node.add_validation(FleetRules(chart, unpinned_https_egress=unpinned_https_egress))
