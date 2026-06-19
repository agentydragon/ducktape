"""Non-graph validation checks for cluster configuration."""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from pathlib import Path

import yaml

from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import HelmReleaseResource, K8sResource
from cluster.validation.kustomize import KustomizeBuildResult


def find_orphaned_files(cluster: ParsedCluster, k8s_dir: Path, candidates: set[Path] | None = None) -> list[str]:
    """Find YAML files not referenced by any kustomization.

    When `candidates` is provided (pre-commit invocation), only files in that
    set are checked — keeps a clean diff from being blocked by orphans a
    parallel agent left on disk but hasn't staged yet. When `candidates` is
    None (CI / Bazel integration test) every YAML under `k8s_dir` is checked.

    Paths in `candidates` must be resolved/absolute to match `all_yaml_files`.
    """
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
        if candidates is not None and yaml_file not in candidates:
            continue
        if yaml_file not in referenced:
            relative = yaml_file.relative_to(k8s_dir)
            errors.append(f"Orphaned file not referenced by any kustomization: {relative}")
    return errors


def check_duplicate_external_secrets(build_results: list[KustomizeBuildResult]) -> list[str]:
    """Check for duplicate external-secrets HelmRelease installations."""
    errors = []
    deployments: dict[str, list[str]] = defaultdict(list)

    for result in build_results:
        for resource in result.resources:
            if isinstance(resource, HelmReleaseResource) and resource.name == "external-secrets":
                key = f"{resource.namespace}/{resource.chart_version or 'unknown'}"
                deployments[key].append(str(result.kustomization_path.parent))

    if len(deployments) > 1:
        errors.append("Multiple external-secrets HelmRelease found:")
        for deployment, paths in deployments.items():
            errors.append(f"  {deployment}: {', '.join(paths)}")
        errors.append("There should be exactly ONE external-secrets installation.")
    elif len(deployments) == 0:
        errors.append("No external-secrets HelmRelease found. At least one is required.")

    return errors


def check_goldilocks_namespace_labels(cluster: ParsedCluster) -> list[str]:
    """Check that namespaces with a goldilocks vpa-update-mode label also have goldilocks enabled."""
    errors = []
    for origin, resource in _goldilocks_check_resources(cluster):
        if resource.kind != "Namespace":
            continue
        labels = resource.metadata.labels
        if (
            "goldilocks.fairwinds.com/vpa-update-mode" in labels
            and labels.get("goldilocks.fairwinds.com/enabled") != "true"
        ):
            errors.append(
                f"{origin}: namespace '{resource.name}' has goldilocks.fairwinds.com/vpa-update-mode "
                f'but is missing goldilocks.fairwinds.com/enabled="true"'
            )
    return errors


_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}
_GOLDILOCKS_ENABLED_LABEL = "goldilocks.fairwinds.com/enabled"


def _goldilocks_check_resources(cluster: ParsedCluster) -> list[tuple[Path, K8sResource]]:
    """Use rendered resources when available so namespace patches are included."""
    if cluster.build_results:
        return [
            (result.kustomization_path, resource) for result in cluster.build_results for resource in result.resources
        ]

    return [
        (file_path, resource) for file_path, resources in cluster.source_resources.items() for resource in resources
    ]


def check_goldilocks_explicit_decision(cluster: ParsedCluster) -> list[str]:
    """Every namespace with workloads must explicitly set goldilocks enabled label."""
    errors = []
    resources = [resource for _, resource in _goldilocks_check_resources(cluster)]

    workload_namespaces: set[str] = set()
    for resource in resources:
        if resource.kind in _WORKLOAD_KINDS and resource.namespace:
            workload_namespaces.add(resource.namespace)

    namespace_goldilocks: dict[str, str | None] = {}
    for resource in resources:
        if resource.kind == "Namespace":
            label = resource.metadata.labels.get(_GOLDILOCKS_ENABLED_LABEL)
            if label is not None or resource.name not in namespace_goldilocks:
                namespace_goldilocks[resource.name] = label

    for ns in sorted(workload_namespaces):
        if ns not in namespace_goldilocks:
            continue
        if namespace_goldilocks[ns] is None:
            errors.append(
                f"Namespace '{ns}' has workloads but is missing explicit "
                f'{_GOLDILOCKS_ENABLED_LABEL} label (set to "true" or "false")'
            )
    return errors


def check_blueprint_completeness(k8s_dir: Path) -> list[str]:
    """Check that all blueprint YAML files are listed in the authentik configMapGenerator."""
    authentik_kust = k8s_dir / "authentik" / "app" / "kustomization.yaml"
    blueprints_dir = k8s_dir / "authentik" / "app" / "blueprints"

    if not authentik_kust.exists():
        raise FileNotFoundError(f"Expected {authentik_kust} to exist")
    if not blueprints_dir.exists():
        raise FileNotFoundError(f"Expected {blueprints_dir} to exist")

    with authentik_kust.open() as f:
        doc = yaml.safe_load(f)

    listed_files: set[str] = set()
    for generator in doc.get("configMapGenerator", []):
        if generator.get("name") == "authentik-sso-blueprints":
            listed_files = {Path(f).name for f in generator.get("files", [])}
            break

    on_disk = {p.name for p in blueprints_dir.glob("*.yaml")}
    unlisted = sorted(on_disk - listed_files)

    if unlisted:
        return [
            f"Authentik blueprint not listed in configMapGenerator: {name}. "
            f"Add 'blueprints/{name}' to the authentik-sso-blueprints files list "
            f"in k8s/authentik/app/kustomization.yaml."
            for name in unlisted
        ]

    return []


@dataclasses.dataclass(frozen=True)
class _BlueprintTag:
    """An authentik blueprint custom YAML tag (e.g. !Find, !KeyOf) and its constructed argument.

    Authentik blueprints use local `!`-tags that `yaml.SafeLoader` rejects; `_BlueprintLoader`
    constructs them into these inert wrappers so the documents parse for static inspection.
    """

    tag: str
    value: object


class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader that tolerates authentik's custom `!`-tags rather than erroring on them."""


def _construct_blueprint_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> _BlueprintTag:
    # Construct the node's children by type — never re-dispatch this node's tag (would recurse forever).
    if isinstance(node, yaml.ScalarNode):
        value: object = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        raise TypeError(f"unexpected YAML node type: {type(node)}")
    return _BlueprintTag(tag_suffix, value)


_BlueprintLoader.add_multi_constructor("!", _construct_blueprint_tag)


def _outpost_provider_name(item: object, id_to_name: dict[str, str]) -> str | None:
    """Resolve one outpost `providers` entry to its proxy-provider name.

    Entries are `!KeyOf <blueprint-id>` (same-document reference) or
    `!Find [authentik_providers_proxy.proxyprovider, [name, <name>]]`.
    """
    if not isinstance(item, _BlueprintTag):
        return None
    if item.tag == "KeyOf" and isinstance(item.value, str):
        return id_to_name.get(item.value)
    if item.tag == "Find" and isinstance(item.value, list) and len(item.value) == 2:
        model, query = item.value
        if (
            model == "authentik_providers_proxy.proxyprovider"
            and isinstance(query, list)
            and len(query) == 2
            and query[0] == "name"
            and isinstance(query[1], str)
        ):
            return query[1]
    return None


def check_proxy_provider_outpost_assignment(k8s_dir: Path) -> list[str]:
    """Every `present` authentik proxy provider must be assigned to an outpost.

    A proxy provider that exists but is on no outpost has no host->backend mapping: the
    embedded outpost doesn't recognise its `external_host` and 302s to the Authentik login
    flow served *on that host*, so "Sign in with Google" builds the OAuth callback against
    the wrong host and Google rejects it with `redirect_uri_mismatch`. This guards the
    wiring that broke `haku.allegedly.works` (and, latently, `tandoor.allegedly.works`).
    """
    blueprints_dir = k8s_dir / "authentik" / "app" / "blueprints"
    if not blueprints_dir.exists():
        raise FileNotFoundError(f"Expected {blueprints_dir} to exist")

    entries = [
        entry
        for path in sorted(blueprints_dir.glob("*.yaml"))
        for doc in yaml.load_all(path.read_text(), Loader=_BlueprintLoader)
        if isinstance(doc, dict)
        for entry in doc.get("entries", [])
        if isinstance(entry, dict)
    ]

    # Map blueprint id -> provider name (for !KeyOf), and collect present providers.
    id_to_name: dict[str, str] = {}
    present_providers: set[str] = set()
    for entry in entries:
        if entry.get("model") != "authentik_providers_proxy.proxyprovider":
            continue
        name = entry.get("identifiers", {}).get("name")
        if not isinstance(name, str):
            continue
        if isinstance(entry.get("id"), str):
            id_to_name[entry["id"]] = name
        if entry.get("state", "present") == "present":
            present_providers.add(name)

    assigned: set[str] = set()
    for entry in entries:
        if entry.get("model") != "authentik_outposts.outpost" or entry.get("state", "present") != "present":
            continue
        for item in entry.get("attrs", {}).get("providers", []):
            if (name := _outpost_provider_name(item, id_to_name)) is not None:
                assigned.add(name)

    return [
        f"Authentik proxy provider '{name}' is defined but assigned to no outpost. Add it to the "
        f"embedded outpost's providers in blueprints/embedded-outpost.yaml (see fava/haku-dashboard); "
        f"otherwise its host 302s to a login flow served on itself and Google SSO fails with "
        f"redirect_uri_mismatch."
        for name in sorted(present_providers - assigned)
    ]
