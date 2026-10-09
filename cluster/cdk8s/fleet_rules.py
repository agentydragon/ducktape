"""Rules every object this repo generates must hold, checked over a chart's rendered
objects when it synthesizes (`App.synth`, `Chart.to_json`, `cdk8s.Testing.synth` all
validate the construct tree first). A violation fails synth with the object named,
instead of an admission denial or a CreateContainerConfigError on the cluster.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import combinations
from typing import Any, cast

import jsii
from cdk8s import ApiObject, Chart
from constructs import IValidation

from cluster.cdk8s.pod_policy import POD_SPEC_PATHS, pod_spec

_OUTSIDE_ENTITIES = frozenset({"world", "remote-node", "host"})
_HTTPS_PORT = "443"
_NETWORKPOLICY_KIND = "CiliumNetworkPolicy"
_CLUSTERWIDE_KIND = "CiliumClusterwideNetworkPolicy"
# Pod-spec keys that name a Secret or ConfigMap: env valueFrom, envFrom, volumes (plain,
# projected sources), imagePullSecrets.
_SECRET_REF_KEYS = frozenset({"secretKeyRef", "secretRef", "imagePullSecrets"})
_CONFIG_MAP_REF_KEYS = frozenset({"configMapKeyRef", "configMapRef"})


def _pod_specs(objects: list[dict[str, Any]]) -> Iterator[tuple[str, dict[str, Any]]]:
    for obj in objects:
        if obj["kind"] in POD_SPEC_PATHS:
            yield f"{obj['kind']}/{obj['metadata']['name']}", pod_spec(obj)


def pod_hardening(objects: list[dict[str, Any]]) -> list[str]:
    """Every pod spec states automountServiceAccountToken, and states true only under a
    ServiceAccount of its own: `default` is shared by every Pod in the namespace that names
    no account. Every container runs under the RuntimeDefault seccomp profile (its own or the
    Pod's) and declares cpu/memory requests and a memory limit. CPU limits are left to
    the namespace LimitRange's default on purpose."""
    errors: list[str] = []
    for name, pod in _pod_specs(objects):
        automount = pod.get("automountServiceAccountToken")
        if automount is None:
            errors.append(f"{name}: automountServiceAccountToken is unset")
        elif automount and pod.get("serviceAccountName", "default") == "default":
            errors.append(f"{name}: automountServiceAccountToken is true under the default ServiceAccount")
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
        if obj["kind"] != _NETWORKPOLICY_KIND or obj["metadata"]["name"] in unpinned:
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


def _outside_https_pins(obj: dict[str, Any]) -> tuple[bool, bool]:
    """Whether this policy's egress to port 443 outside the cluster pins TLS SNI: `(pins_sni,
    left_open)`, each true when at least one such rule carries `serverNames` or leaves it off. A
    destination is outside the cluster when it names FQDNs or entities from `_OUTSIDE_ENTITIES`;
    in-cluster peers (`toEndpoints`, and `toEntities: [cluster]`) never are."""
    pins_sni = left_open = False
    for rule in obj["spec"].get("egress", []):
        if not (rule.get("toFQDNs") or _OUTSIDE_ENTITIES & set(rule.get("toEntities", []))):
            continue
        for to_ports in rule.get("toPorts", []):
            if not any(port["port"] == _HTTPS_PORT for port in to_ports.get("ports", [])):
                continue
            pinned = bool(to_ports.get("serverNames"))
            pins_sni, left_open = pins_sni or pinned, left_open or not pinned
    return pins_sni, left_open


def _endpoint_scope(obj: dict[str, Any]) -> tuple[str, dict[str, str], bool]:
    """What a policy's `endpointSelector` covers: `(namespace, matchLabels, selects_everything)`.

    A policy that names no labels, or names `matchExpressions`, cannot be reduced to label pairs
    here, so it answers `selects_everything`: it conflicts rather than being skipped quietly.
    `CiliumClusterwideNetworkPolicy` has no namespace of its own and reaches every one, so it
    carries the empty string, which `_share_endpoints` reads as "any".
    """
    selector = obj["spec"].get("endpointSelector") or {}
    labels = dict(selector.get("matchLabels") or {})
    namespace = "" if obj["kind"] == _CLUSTERWIDE_KIND else obj["metadata"].get("namespace", "")
    return namespace, labels, not labels or bool(selector.get("matchExpressions"))


def _share_endpoints(left: tuple[str, dict[str, str], bool], right: tuple[str, dict[str, str], bool]) -> bool:
    """Whether one endpoint can satisfy both selectors at once.

    A `matchLabels` selector matches every endpoint carrying at least those labels, so two
    selectors overlap unless they pin the same key to different values: an endpoint labelled with
    the union of both satisfies them, however narrow each is on its own. Disjoint key sets always
    overlap, which is the answer a check for shared keys alone would get wrong.
    """
    left_namespace, left_labels, left_all = left
    right_namespace, right_labels, right_all = right
    if left_namespace and right_namespace and left_namespace != right_namespace:
        return False
    if left_all or right_all:
        return True
    shared = set(left_labels) & set(right_labels)
    return all(left_labels[key] == right_labels[key] for key in shared)


def https_egress_sni_conflicts(objects: list[dict[str, Any]]) -> list[str]:
    """No endpoint may pin SNI on outside port 443 and leave that port open at the same time.

    `serverNames` is an L7 rule, and Cilium enforces L7 per **port on the endpoint**, over the
    merge of every rule and every policy selecting that endpoint. Once anything pins SNI on 443,
    the endpoint's node-IP HTTPS is decided by the union of the `serverNames` lists in scope: an
    open `toEntities: [world, remote-node, host]:443` rule -- in the same policy or in a sibling
    one -- stops being open for every name nobody typed into a list, and those connections reset
    inside the handshake. Each rule reads correctly on its own and the pair means something else
    entirely, so the combination is refused at synth rather than discovered later as reset
    handshakes to whichever public hostname is missing. Two weeks of Grocy, Forgejo, Haku's
    mailbox, ActivityWatch and aiquota traffic failing out of agentplane-staging sandboxes was
    exactly this pair: cluster/docs/cilium_network_policy.md section "An SNI rule decides port 443
    for every policy that selects the same Pod".

    Two policies are compared when one endpoint can satisfy both selectors. Namespaced policies
    must share a namespace to do it -- `CiliumNetworkPolicy` cannot reach another one -- and a
    clusterwide policy is compared against every namespace it covers.
    """
    scoped = [
        (f"{obj['kind']}/{obj['metadata']['name']}", _endpoint_scope(obj), _outside_https_pins(obj))
        for obj in objects
        if obj["kind"] in (_NETWORKPOLICY_KIND, _CLUSTERWIDE_KIND)
    ]
    errors = [
        f"{name} pins SNI on outside HTTPS and leaves port {_HTTPS_PORT} open beside it: the pin "
        "decides every name on that port, not just the ones it lists"
        for name, _, (pins_sni, left_open) in scoped
        if pins_sni and left_open
    ]
    for first, second in combinations(scoped, 2):
        (name, scope, first_pins), (other, other_scope, other_pins) = first, second
        if name == other or not _share_endpoints(scope, other_scope):
            continue
        pinned, unpinned = _pin_against_open((name, first_pins), (other, other_pins))
        if pinned is not None:
            errors.append(
                f"{pinned} pins SNI on outside HTTPS while {unpinned} leaves port {_HTTPS_PORT} open "
                "for the same endpoints: the pin decides every name on that port, not just its own"
            )
    return errors


def _pin_against_open(
    first: tuple[str, tuple[bool, bool]], second: tuple[str, tuple[bool, bool]]
) -> tuple[str | None, str | None]:
    """Which of two policies pins SNI on outside 443 and which leaves it open, or `(None, None)`
    when they do not contradict each other."""
    (first_name, (first_pins_sni, first_open)), (second_name, (second_pins_sni, second_open)) = first, second
    if first_pins_sni and second_open:
        return first_name, second_name
    if second_pins_sni and first_open:
        return second_name, first_name
    return None, None


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
            *https_egress_sni_conflicts(objects),
            *resolved_references(objects),
        ]


def add_fleet_rules(chart: Chart, *, unpinned_https_egress: frozenset[str] = frozenset()) -> None:
    chart.node.add_validation(FleetRules(chart, unpinned_https_egress=unpinned_https_egress))
