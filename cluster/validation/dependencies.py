"""Flux dependency-graph checks: operator prerequisites and sourceRef resolution."""

from __future__ import annotations

from pathlib import Path

import networkx as nx

from cluster.validation.cluster import ParsedCluster
from cluster.validation.crd_layering import CRD_TO_OPERATOR
from cluster.validation.flux import EXTERNAL_ARTIFACT_KIND


def validate_operator_dependencies(
    cluster: ParsedCluster, repo_root: Path, crd_to_operator: dict[str, str] | None = None
) -> list[str]:
    """Validate that kustomizations using CRD instances transitively depend on the managing operator.

    Uses CRD_TO_OPERATOR from crd_layering.py as the source of truth for which CRD kinds
    require which operator prerequisite.
    """
    if crd_to_operator is None:
        crd_to_operator = CRD_TO_OPERATOR

    flux_resources = cluster.flux_kust_resources(repo_root)
    errors = []
    g = cluster.graph
    reported: set[tuple[str, str]] = set()

    for kust_name, resources in flux_resources.items():
        for resource in resources:
            operator = crd_to_operator.get(resource.kind)
            if operator is None:
                continue
            key = (kust_name, operator)
            if key in reported:
                continue
            # A HelmRelease is admitted before helm-controller installs its operator.
            # A zero-length graph path cannot order that install before sibling CRs.
            # Providers applying resources directly (e.g. Flux image automation) do
            # not have this asynchronous Helm installation boundary.
            if kust_name == operator and any(r.kind == "HelmRelease" for r in resources):
                errors.append(
                    f"{kust_name} installs its operator through Helm and also applies {resource.kind} resources; "
                    f"move those instances to a separate Kustomization that depends on {operator}"
                )
                reported.add(key)
            elif operator not in g or not nx.has_path(g, kust_name, operator):
                errors.append(
                    f"{kust_name} uses {resource.kind} resources but doesn't transitively depend on {operator}"
                )
                reported.add(key)

    return errors


def check_source_references(cluster: ParsedCluster) -> list[str]:
    """Fail sourceRef entries that name no source Flux can resolve.

    Flux resolves a bare sourceRef (no ``namespace:``) in the Kustomization's own
    namespace; if the source lives in a different namespace the reference silently
    misses and the Kustomization stalls — the PR #3759 outage class. Names absent
    from this repo altogether are skipped: the validator can't see them.

    An ExternalArtifact exists only as an ArtifactGenerator's output, so its sourceRef
    must name an artifact a generator declares in that namespace — a ref that names
    nothing a generator produces stalls with ArtifactFailed and takes every dependent
    with it (the #6297 outage class).
    """
    sources = cluster.flux_sources
    errors: list[str] = []
    for name, spec in cluster.flux_kustomizations.items():
        sr = spec.source_ref
        if not (spec.namespace and sr and sr.name):
            continue
        target_ns = sr.namespace or spec.namespace
        if (sr.kind, target_ns, sr.name) in sources:
            continue
        if sr.kind == EXTERNAL_ARTIFACT_KIND:
            declared = sorted(n for kind, ns, n in sources if kind == EXTERNAL_ARTIFACT_KIND and ns == target_ns)
            errors.append(
                f"{name} (ns={spec.namespace}) sourceRef ExternalArtifact '{sr.name}' resolves to "
                f"ns={target_ns} but no ArtifactGenerator declares that artifact there (declared: "
                f"{declared}); an ExternalArtifact exists only as a generator's output."
            )
        elif any(kind == sr.kind and n == sr.name for kind, _, n in sources):
            errors.append(
                f"{name} (ns={spec.namespace}) sourceRef '{sr.name}' resolves to "
                f"ns={target_ns} but no source exists there; add 'namespace:' "
                f"to the sourceRef."
            )
    return errors


def validate_dependencies(cluster: ParsedCluster, repo_root: Path) -> list[str]:
    """Validate operator prerequisites and sourceRef resolution across the Flux graph."""
    if not cluster.flux_kustomizations:
        return ["No Flux kustomizations found"]
    return [*validate_operator_dependencies(cluster, repo_root), *check_source_references(cluster)]
