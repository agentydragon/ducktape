"""Non-graph validation checks for cluster configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cluster.cdk8s.cilium_rule_sections import enforces_nothing
from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import (
    AgentplaneSandboxTemplateResource,
    CiliumPolicyResource,
    ClusterExternalSecretResource,
    CronJobResource,
    ExternalSecretResource,
    K8sResource,
    PodTemplateWorkloadResource,
    SandboxTemplateResource,
    SecretResource,
    SecretStoreResource,
)

_FORGEJO_REGISTRY = "git.allegedly.works"
_FORGEJO_CREDENTIAL_SECRET = "forgejo-images-creds"
_FORGEJO_IMAGE_WORKLOAD_TYPES = (
    CronJobResource,
    PodTemplateWorkloadResource,
    SandboxTemplateResource,
    AgentplaneSandboxTemplateResource,
)
# These inputs are intentionally stored without a deploy Kustomization reference:
# Dreo is retained for future Home Assistant provisioning (see homeassistant/TODO.md).
_INTENTIONALLY_STORED_ONLY_FILES = {Path("cluster/k8s/external-creds/dreo-account.sops.yaml")}


def find_orphaned_files(cluster: ParsedCluster, repo_root: Path) -> list[str]:
    """Find YAML files not referenced by any kustomization, except stored-only inputs."""
    referenced: set[Path] = set()
    for kust in cluster.kustomize_files.values():
        referenced.update(kust.all_referenced_files)
        for resource in kust.resolved_resources:
            if resource.is_dir():
                referenced.add(resource / "kustomization.yaml")

    errors = []
    for yaml_file in cluster.all_yaml_files:
        if yaml_file.name == "kustomization.yaml":
            continue
        relative = yaml_file.relative_to(repo_root)
        if relative in _INTENTIONALLY_STORED_ONLY_FILES:
            continue
        if yaml_file not in referenced:
            errors.append(f"Orphaned file not referenced by any kustomization: {relative}")
    return errors


def check_external_credential_ownership(cluster: ParsedCluster) -> list[str]:
    """Only the shared external-creds ClusterSecretStore reads ducktape-flux."""
    store_owner = "external-secrets-config"
    store_name = "kubernetes-external-creds-secret-store"
    resources_by_kustomization = cluster.flux_kust_resources()
    errors: list[str] = []
    stores: list[SecretStoreResource] = []
    for kustomization, resources in resources_by_kustomization.items():
        for resource in resources:
            if not isinstance(resource, SecretStoreResource):
                continue
            provider = resource.spec.provider.kubernetes
            if provider is None or provider.remote_namespace != "ducktape-flux":
                continue
            if resource.kind != "ClusterSecretStore" or resource.name != store_name or kustomization != store_owner:
                errors.append(
                    f"{kustomization} {resource.kind} '{resource.namespace}/{resource.name}' reads "
                    f"external-creds; use {store_owner}'s shared ClusterSecretStore '{store_name}'"
                )
            else:
                stores.append(resource)

    if len(stores) != 1:
        errors.append(f"expected exactly one {store_owner} ClusterSecretStore '{store_name}', found {len(stores)}")

    return errors


def _selector_matches(selector: dict[str, Any], labels: dict[str, str]) -> bool | None:
    """Match known labels; absent labels can still be added outside the render."""
    unknown = False
    for key, value in selector.get("matchLabels", {}).items():
        if key not in labels:
            unknown = True
        elif labels[key] != value:
            return False
    for expression in selector.get("matchExpressions", []):
        key, operator = expression["key"], expression["operator"]
        values = expression.get("values", [])
        if operator not in ("In", "NotIn", "Exists", "DoesNotExist"):
            raise ValueError(f"unsupported namespace selector operator {operator!r}")
        if key not in labels:
            unknown = True
            continue
        if operator == "In" and labels[key] not in values:
            return False
        if operator == "NotIn" and labels[key] in values:
            return False
        if operator == "DoesNotExist":
            return False
    return None if unknown else True


def _store_allows_namespace(store: SecretStoreResource, namespace: str, labels: dict[str, str] | None) -> bool | None:
    """Return None if a selector needs namespace labels unavailable in the rendered tree."""
    if store.kind != "ClusterSecretStore" or not store.spec.conditions:
        return True
    unknown = False
    for condition in store.spec.conditions:
        if namespace in condition.get("namespaces", []):
            return True
        selector = condition.get("namespaceSelector")
        if selector is not None:
            known_labels = labels if labels is not None else {"kubernetes.io/metadata.name": namespace}
            matched = _selector_matches(selector, known_labels)
            if matched is True:
                return True
            if matched is None:
                unknown = True
        for pattern in condition.get("namespaceRegexes", []):
            try:
                if re.search(pattern, namespace):
                    return True
            except re.error:
                # ESO uses Go's regexp syntax; a Python parse failure is not a denial.
                unknown = True
    return None if unknown else False


def _secret_store_refs(spec: dict[str, Any]) -> set[tuple[str, str]]:
    """Stores actually selected by data entries; per-entry sources override the default."""
    refs = []
    for entry in [*spec.get("data", []), *spec.get("dataFrom", [])]:
        source = entry.get("sourceRef") or {}
        if source.get("generatorRef"):
            continue
        refs.append(source.get("storeRef") or spec.get("secretStoreRef") or {})
    return {(ref.get("kind", "SecretStore"), ref.get("name", "")) for ref in refs}


@dataclass(frozen=True)
class ExternalSecretWiringReport:
    errors: list[str]
    unresolved: list[str]


def check_external_secret_store_wiring(cluster: ParsedCluster) -> ExternalSecretWiringReport:
    """Check rendered ESO consumers against rendered stores and referent service accounts.

    ClusterExternalSecret selectors are evaluated for namespaces visible in the render.
    This cannot establish the labels of namespaces created or modified outside GitOps.
    """
    resources = _rendered_resources_with_origin(cluster)
    namespaces: dict[str, dict[str, str] | None] = {}
    for _, resource in resources:
        if resource.kind != "Namespace":
            continue
        labels = {**resource.metadata.labels, "kubernetes.io/metadata.name": resource.name}
        if resource.name not in namespaces:
            namespaces[resource.name] = labels
        elif namespaces[resource.name] != labels:
            # Multiple Flux owners can contribute labels to one live Namespace.
            # Their rendered objects do not establish the final label set.
            namespaces[resource.name] = None
    stores = {
        (resource.kind, resource.namespace if resource.kind == "SecretStore" else "", resource.name): resource
        for _, resource in resources
        if isinstance(resource, SecretStoreResource)
    }
    service_accounts = {
        (resource.namespace, resource.name) for _, resource in resources if resource.kind == "ServiceAccount"
    }
    consumers: list[tuple[str, K8sResource, str, dict[str, Any]]] = []
    unresolved: set[str] = set()
    for origin, resource in resources:
        if isinstance(resource, ExternalSecretResource):
            if resource.namespace:
                consumers.append((origin, resource, resource.namespace, resource.spec))
            else:
                unresolved.add(f"{origin}: ExternalSecret '{resource.name}' has no rendered namespace")
        elif isinstance(resource, ClusterExternalSecretResource):
            selected = set(resource.spec.get("namespaces", []))
            selectors = list(resource.spec.get("namespaceSelectors", []))
            if (selector := resource.spec.get("namespaceSelector")) is not None:
                selectors.append(selector)
            if selectors:
                unresolved.add(
                    f"{origin}: ClusterExternalSecret '{resource.name}' namespace selectors cannot cover "
                    "namespaces or labels not established by the render"
                )
            for name, labels in namespaces.items():
                known_labels = labels if labels is not None else {"kubernetes.io/metadata.name": name}
                matches = [_selector_matches(selector, known_labels) for selector in selectors]
                if True in matches:
                    selected.add(name)
                elif None in matches:
                    unresolved.add(
                        f"{origin}: ClusterExternalSecret '{resource.name}' cannot resolve selector "
                        f"for Namespace '{name}' from rendered labels"
                    )
            consumers.extend(
                (origin, resource, namespace, resource.spec.get("externalSecretSpec", {}))
                for namespace in sorted(selected)
            )

    errors: list[str] = []
    for origin, resource, namespace, spec in consumers:
        for kind, name in sorted(_secret_store_refs(spec)):
            consumer = f"{origin}: {resource.kind} '{resource.name}' in namespace '{namespace}'"
            if not name:
                errors.append(f"{consumer} has a data entry without a store reference")
                continue
            store = stores.get((kind, namespace if kind == "SecretStore" else "", name))
            if store is None:
                errors.append(f"{consumer} references missing {kind} '{name}'")
                continue
            allowed = _store_allows_namespace(store, namespace, namespaces.get(namespace))
            if allowed is False:
                errors.append(f"{consumer} is denied by {kind} '{name}' namespace conditions")
            elif allowed is None:
                unresolved.add(f"{consumer}: {kind} '{name}' namespace conditions cannot be evaluated from the render")
            provider = store.spec.provider.kubernetes
            service_account = provider.auth.get("serviceAccount") if provider else None
            if service_account and not service_account.get("namespace"):
                account_name = service_account.get("name")
                if account_name and (namespace, account_name) not in service_accounts:
                    errors.append(
                        f"{consumer} uses {kind} '{name}' with referent ServiceAccount "
                        f"'{namespace}/{account_name}' missing from the rendered tree"
                    )
    return ExternalSecretWiringReport(errors=errors, unresolved=sorted(unresolved))


def check_goldilocks_namespace_labels(cluster: ParsedCluster) -> list[str]:
    """A namespace with a Goldilocks update mode is not opted out of Goldilocks, which would ignore
    the mode. Goldilocks runs on by default, so the mode needs no `enabled: "true"` beside it."""
    errors = []
    for origin, resource in _rendered_resources_with_origin(cluster):
        if resource.kind != "Namespace":
            continue
        labels = resource.metadata.labels
        if _VPA_UPDATE_MODE_LABEL in labels and labels.get(_GOLDILOCKS_ENABLED_LABEL, "true") != "true":
            errors.append(
                f"{origin}: namespace '{resource.name}' has {_VPA_UPDATE_MODE_LABEL} "
                f'but is labeled {_GOLDILOCKS_ENABLED_LABEL}: "{labels[_GOLDILOCKS_ENABLED_LABEL]}"'
            )
    return errors


_GOLDILOCKS_ENABLED_LABEL = "goldilocks.fairwinds.com/enabled"
_VPA_UPDATE_MODE_LABEL = "goldilocks.fairwinds.com/vpa-update-mode"


def _rendered_resources_with_origin(cluster: ParsedCluster) -> list[tuple[str, K8sResource]]:
    return [
        (f"{owner} ({cluster.flux_kustomizations[owner].path})", resource)
        for owner, resources in cluster.rendered_resources.items()
        for resource in resources
    ]


def check_forgejo_image_namespace_reflection(cluster: ParsedCluster) -> list[str]:
    """Every rendered Forgejo image workload namespace has its own forgejo-images-creds ExternalSecret."""
    resources = _rendered_resources_with_origin(cluster)
    namespaces_with_credential = {
        resource.namespace
        for _, resource in resources
        if isinstance(resource, ExternalSecretResource) and resource.name == _FORGEJO_CREDENTIAL_SECRET
    }
    errors: list[str] = []
    for origin, resource in resources:
        if not isinstance(resource, _FORGEJO_IMAGE_WORKLOAD_TYPES):
            continue
        images = {image for image in resource.images if image.split("/", 1)[0] == _FORGEJO_REGISTRY}
        if not images:
            continue
        if resource.namespace not in namespaces_with_credential:
            errors.append(
                f"{origin}: {resource.kind} '{resource.namespace}/{resource.name}' runs Forgejo image(s) "
                f"{', '.join(sorted(images))}, but namespace '{resource.namespace}' has no "
                f"ExternalSecret named {_FORGEJO_CREDENTIAL_SECRET}"
            )
    return errors


def check_sops_decryption_blocks(cluster: ParsedCluster) -> list[str]:
    """Active Flux Kustomizations that render a SOPS-encrypted Secret must declare
    spec.decryption with provider: sops AND a secretRef.name — otherwise Flux applies
    the ENC[...] ciphertext literally (no provider) or has no age key to decrypt with
    (no secretRef). Both fail silently. Build-level: inspects what Flux actually
    applies, so it neither over-counts SOPS files in sibling/child kustomizations nor
    misses those pulled in via nested kustomize refs."""
    errors: list[str] = []
    for name, resources in cluster.flux_kust_resources().items():
        if not any(isinstance(r, SecretResource) and r.sops is not None for r in resources):
            continue
        spec = cluster.flux_kustomizations[name]
        dec = spec.decryption
        if dec is None or dec.provider != "sops":
            errors.append(
                f"Flux Kustomization '{name}' renders a SOPS-encrypted Secret but has no "
                f"spec.decryption.provider: sops. Without it Flux applies the ENC[...] "
                f"ciphertext literally — add a decryption block pointing at sops-age-cluster-secrets."
            )
        elif dec.secret_ref is None or not dec.secret_ref.name:
            errors.append(
                f"Flux Kustomization '{name}' declares decryption.provider: sops but no "
                f"spec.decryption.secretRef.name — Flux has no age key to decrypt with, so the "
                f"Secret's ENC[...] ciphertext is applied literally. Add a secretRef pointing at "
                f"sops-age-cluster-secrets."
            )
    return errors


def check_cilium_policy_rules_nonempty(cluster: ParsedCluster) -> list[str]:
    """No Cilium policy rule `enforces_nothing`. The fleet rules refuse a generated one at
    synth; this holds hand-written policies and anything a kustomize patch changes after synth."""
    errors = []
    for origin, resource in _rendered_resources_with_origin(cluster):
        if not isinstance(resource, CiliumPolicyResource):
            continue
        for index, rule in enumerate(resource.rules):
            if enforces_nothing(rule):
                errors.append(
                    f"{origin}: {resource.kind} '{resource.name}' rule {index} has no non-empty "
                    f"ingress/ingressDeny/egress/egressDeny section — Cilium rejects it (Valid=False) "
                    f"and enforces nothing. For default-deny use one empty rule element, e.g. "
                    f"`ingress: [{{}}]`, never an empty list."
                )
    return errors
