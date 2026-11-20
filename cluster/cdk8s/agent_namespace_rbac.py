"""GitOps-owned diagnostics bindings from the same policy as managed grants.

Labels alone grant no access.
"""

from functools import partial
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from cluster.cdk8s import agent_access_profiles as access
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespace_access import NAMESPACE_DIAGNOSTICS, AgentReadable


def directory(namespace: str) -> str:
    return f"{GENERATED_ROOT}/agents/namespace-rbac/{namespace}"


def chart(app: App, namespace: str) -> Chart:
    classification = NAMESPACE_DIAGNOSTICS[namespace]
    scope = Chart(app, f"agent-namespace-rbac-{namespace}", disable_resource_name_hashes=True)
    for level in ("metadata", "logs") if classification is AgentReadable.LOGS else ("metadata",):
        k8s.KubeRoleBinding(
            scope,
            level,
            metadata=k8s.ObjectMeta(name=f"agent-diagnostics-{level}", namespace=namespace),
            role_ref=access.role_ref(f"{namespace}-{level}"),
            subjects=[subject.k8s() for subject in access.NAMESPACE_READER_SUBJECTS],
        )
    return scope


def write_manifests(root: Path) -> None:
    for namespace in NAMESPACE_DIAGNOSTICS:
        output = directory(namespace)
        manifest = write_charts(root, output, partial(chart, namespace=namespace))
        write_yaml(root / output / "kustomization.yaml", kustomize_kustomization(resources=[manifest]))


def add_flux_kustomizations(flux_chart: Chart, roles: Kustomization) -> None:
    # Namespace existence is enforced by the API when the RoleBindings are applied.
    # On bootstrap Flux retries until the namespace exists; application readiness
    # must never gate the diagnostic access needed to investigate an unhealthy app.
    # Do not emit Namespaces here: their existing owners retain lifecycle control.
    source = KustomizationSpecSourceRef(
        kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="ducktape", namespace="ducktape-flux"
    )
    for namespace in NAMESPACE_DIAGNOSTICS:
        flux_kustomization(
            flux_chart,
            f"agent-namespace-rbac-{namespace}",
            source,
            path=f"./{directory(namespace)}",
            depends_on=[flux_kustomization_depends_on(roles)],
            description=f"Static agent diagnostics in {namespace}; policy shared with managed agents.",
        )
