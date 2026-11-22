"""Per-target-namespace delegation for the Policy Controller's assignable RoleBindings.

Each external namespace has its own Flux Kustomization. An absent or suspended
service namespace cannot make the Agentplane app Kustomization fail to apply.
Preset and standing-account role grants plus retained cleanup scopes determine the target roles.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import partial
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from cluster.cdk8s.agentplane import policy_controller
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.kubernetes_grants import KubernetesGrant, RoleBindingGrant
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

_RBAC_GROUP = "rbac.authorization.k8s.io"


def external_charts(
    env: Environment, assignable_grants: Sequence[KubernetesGrant]
) -> dict[str, Callable[[App], Chart]]:
    """One chart per external namespace, including cleanup scopes that no longer grant any Roles."""
    roles: dict[str, set[str]] = {namespace: set() for namespace in env.kubernetes_binding_cleanup_namespaces}
    for grant in assignable_grants:
        if isinstance(grant, RoleBindingGrant):
            names = roles.setdefault(grant.namespace, set())
            if grant.role_ref.kind == "Role":
                names.add(grant.role_ref.name)
    return {
        namespace: partial(_chart, env=env, target_namespace=namespace, role_names=sorted(roles[namespace]))
        for namespace in sorted(roles.keys() - {env.namespace})
    }


def directory(env: Environment, target_namespace: str) -> str:
    return f"{GENERATED_ROOT}/agentplane/binding-delegation/{env.namespace}/{target_namespace}"


def _chart(app: App, env: Environment, target_namespace: str, role_names: Sequence[str]) -> Chart:
    scope = Chart(app, f"{env.namespace}-binding-delegation-{target_namespace}", disable_resource_name_hashes=True)
    role_name = f"{env.namespace}-external-bindings"
    k8s.KubeRole(
        scope,
        "role",
        metadata=k8s.ObjectMeta(name=role_name, namespace=target_namespace),
        rules=[
            k8s.PolicyRule(
                api_groups=[_RBAC_GROUP], resources=["rolebindings"], verbs=["create", "delete", "get", "list", "patch"]
            ),
            # Persisted bindings can retain Role names after catalog removal.
            k8s.PolicyRule(api_groups=[_RBAC_GROUP], resources=["roles"], verbs=["get"]),
            *[
                k8s.PolicyRule(api_groups=[_RBAC_GROUP], resources=["roles"], resource_names=[name], verbs=["bind"])
                for name in role_names
            ],
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "role-binding",
        metadata=k8s.ObjectMeta(name=role_name, namespace=target_namespace),
        role_ref=k8s.RoleRef(api_group=_RBAC_GROUP, kind="Role", name=role_name),
        subjects=[k8s.Subject(kind="ServiceAccount", name=policy_controller.NAME, namespace=env.namespace)],
    )
    return scope


def write_manifests(root: Path, env: Environment, charts: Mapping[str, Callable[[App], Chart]]) -> None:
    for target_namespace, build in charts.items():
        output_dir = directory(env, target_namespace)
        manifest = write_charts(root, output_dir, build)
        write_yaml(root / output_dir / "kustomization.yaml", kustomize_kustomization(resources=[manifest]))


def add_flux_kustomizations(
    flux_chart: Chart,
    env: Environment,
    target_dependencies: Mapping[str, Kustomization],
    target_namespaces: Iterable[str],
) -> None:
    """Each target owner gates its delegation; bootstrap roots have no generated owner."""
    source = KustomizationSpecSourceRef(
        kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="ducktape", namespace="ducktape-flux"
    )
    for target_namespace in target_namespaces:
        # Bootstrap creates these namespaces before the generated owners reconcile.
        depends_on = (
            None
            if target_namespace in {"ducktape-flux", "flux-system"}
            else [flux_kustomization_depends_on(target_dependencies[target_namespace])]
        )
        flux_kustomization(
            flux_chart,
            f"{env.namespace}-binding-delegation-{target_namespace}",
            source,
            path=f"./{directory(env, target_namespace)}",
            depends_on=depends_on,
            description=f"Delegates managed RoleBinding reconciliation in {target_namespace}.",
        )
