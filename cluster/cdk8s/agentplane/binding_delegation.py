"""Per-target-namespace delegation for Agentplane-managed Sandbox RoleBindings.

Each external namespace has its own Flux Kustomization. An absent or suspended
service namespace cannot make the Agentplane app Kustomization fail to apply.
The catalog and retained cleanup scopes remain the only scope inputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from agentplane.app.kubernetes_grants import RoleBindingGrant
from cluster.cdk8s.agentplane.app import NAME as APP_SERVICE_ACCOUNT_NAME
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

_RBAC_GROUP = "rbac.authorization.k8s.io"


def external_scopes(env: Environment) -> tuple[str, ...]:
    """Keep delegation for old bindings after their catalog entry is removed."""
    scopes = {
        grant.namespace for grant in env.app_config.kubernetes_grants.values() if isinstance(grant, RoleBindingGrant)
    } | set(env.app_config.kubernetes_binding_cleanup_namespaces)
    return tuple(sorted(scopes - {env.namespace}))


def directory(env: Environment, target_namespace: str) -> str:
    return f"{GENERATED_ROOT}/agentplane/binding-delegation/{env.namespace}/{target_namespace}"


def _role_name(env: Environment) -> str:
    return f"{env.namespace}-external-bindings"


def chart(app: App, env: Environment, target_namespace: str) -> Chart:
    if target_namespace not in external_scopes(env):
        raise ValueError(f"{target_namespace!r} is not an external managed binding scope")
    scope = Chart(app, f"{env.namespace}-binding-delegation-{target_namespace}", disable_resource_name_hashes=True)
    role_name = _role_name(env)
    role_names = sorted(
        {
            grant.role_ref.name
            for grant in env.app_config.kubernetes_grants.values()
            if isinstance(grant, RoleBindingGrant)
            and grant.namespace == target_namespace
            and grant.role_ref.kind == "Role"
        }
    )
    k8s.KubeRole(
        scope,
        "role",
        metadata=k8s.ObjectMeta(name=role_name, namespace=target_namespace),
        rules=[
            k8s.PolicyRule(
                api_groups=[_RBAC_GROUP], resources=["rolebindings"], verbs=["create", "get", "list", "delete"]
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
        subjects=[k8s.Subject(kind="ServiceAccount", name=APP_SERVICE_ACCOUNT_NAME, namespace=env.namespace)],
    )
    return scope


def write_manifests(root: Path, env: Environment) -> None:
    for target_namespace in external_scopes(env):
        output_dir = directory(env, target_namespace)
        manifest = write_charts(root, output_dir, partial(chart, env=env, target_namespace=target_namespace))
        write_yaml(root / output_dir / "kustomization.yaml", kustomize_kustomization(resources=[manifest]))


def add_flux_kustomizations(
    flux_chart: Chart, env: Environment, target_dependencies: Mapping[str, Kustomization | None]
) -> None:
    """Each target owner gates its delegation; bootstrap roots have no generated owner."""
    scopes = external_scopes(env)
    if missing := set(scopes) - target_dependencies.keys():
        raise ValueError(f"managed binding scopes lack namespace dependencies: {sorted(missing)}")
    bootstrap_scopes = {"flux-system", "ducktape-flux"}
    if missing := {scope for scope in scopes if scope not in bootstrap_scopes and target_dependencies[scope] is None}:
        raise ValueError(f"managed binding scopes lack namespace dependencies: {sorted(missing)}")
    if unexpected := {
        scope for scope in scopes if scope in bootstrap_scopes and target_dependencies[scope] is not None
    }:
        raise ValueError(f"bootstrap namespaces have no generated Flux dependency: {sorted(unexpected)}")
    source = KustomizationSpecSourceRef(
        kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="ducktape", namespace="ducktape-flux"
    )
    for target_namespace in scopes:
        dependency = target_dependencies[target_namespace]
        flux_kustomization(
            flux_chart,
            f"{env.namespace}-binding-delegation-{target_namespace}",
            source,
            path=f"./{directory(env, target_namespace)}",
            depends_on=[flux_kustomization_depends_on(dependency)] if dependency is not None else None,
            description=f"Delegates managed Sandbox RoleBinding reconciliation in {target_namespace}.",
        )
