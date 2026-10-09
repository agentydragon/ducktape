"""The Grocy user→permission reconciler shared by every household (`user-perms-base`): the
one-shot provisioner Job, the daily drift-correcting CronJob, the NetworkPolicy admitting both
to Grocy, and the policy they apply. Each household's `<household>/user-perms` places the base
in its namespace.

The base's `kustomization.yaml` hash-suffixes the policy's ConfigMap, so a policy edit changes
the Job spec and the `force` annotation makes Flux recreate it. It includes the hand-written
`PINS_DIR` Component across the roots, which overrides the reconciler's "unset" placeholder
tag (cluster/cdk8s/AGENTS.md § the `:tag` Setters marker).
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import ConfigMapArgs, kustomize_kustomization
from cluster.cdk8s.forgejo_registry.chart import SECRET_NAME
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.grocy import app as grocy  # `app` is the cdk8s App parameter here
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.provisioners.grocy_user_perms.provision import Policy

BASE_DIR = f"{grocy.ROOT}/user-perms-base"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/grocy/user-perms-image-pins"
_NAME = "grocy-user-perms-provisioner"
# Shared by the Job's and the CronJob's pods: the NetworkPolicy admits them to grocy:80.
_LABELS = {"app.kubernetes.io/name": _NAME}
# Script baked in via Bazel (//cluster/provisioners/grocy_user_perms:image).
_IMAGE = "git.allegedly.works/ducktape-ci/grocy-user-perms-provisioner:unset"
_POLICY_CONFIG_MAP = "grocy-user-perms-policy"
_POLICY_DIR = "/config"
_POLICY_FILE = "policy.yaml"
# Every row of Grocy's `permission_hierarchy`. `User::HasPermission` looks a permission up by
# exact name and does not walk the hierarchy: a user holding only ADMIN still fails a check for
# MASTER_DATA_EDIT or STOCK_PURCHASE (seen live 2026-07-12: a user converged to exactly {ADMIN}
# got 403 on both), so full access is every row. The reconciler fails on a name Grocy lacks; a
# permission Grocy adds stays ungranted until it is added here.
_FULL_ACCESS = frozenset(
    {
        "ADMIN",
        "USERS",
        "USERS_CREATE",
        "USERS_EDIT",
        "USERS_READ",
        "USERS_EDIT_SELF",
        "STOCK",
        "SHOPPINGLIST",
        "RECIPES",
        "CHORES",
        "BATTERIES",
        "TASKS",
        "EQUIPMENT",
        "CALENDAR",
        "STOCK_PURCHASE",
        "STOCK_CONSUME",
        "STOCK_INVENTORY",
        "STOCK_TRANSFER",
        "STOCK_OPEN",
        "STOCK_EDIT",
        "SHOPPINGLIST_ITEMS_ADD",
        "SHOPPINGLIST_ITEMS_DELETE",
        "RECIPES_MEALPLAN",
        "CHORE_TRACK_EXECUTION",
        "CHORE_UNDO_EXECUTION",
        "BATTERIES_TRACK_CHARGE_CYCLE",
        "BATTERIES_UNDO_CHARGE_CYCLE",
        "TASKS_UNDO_EXECUTION",
        "TASKS_MARK_COMPLETED",
        "MASTER_DATA_EDIT",
    }
)
_POLICY = Policy(
    users={
        "agentydragon": _FULL_ACCESS,
        "auragon": _FULL_ACCESS,
        # Explicitly empty: haku stays read-only, converged back even if elevated in the UI.
        "haku": set(),
    }
)


def output_dir(household: str) -> str:
    return f"{grocy.ROOT}/{household}/user-perms"


def _pod_spec(container_name: str) -> k8s.PodSpec:
    return k8s.PodSpec(
        restart_policy="OnFailure",
        automount_service_account_token=False,
        image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
        security_context=k8s.PodSecurityContext(
            run_as_non_root=True,
            run_as_user=1000,
            run_as_group=1000,
            seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
        ),
        volumes=[k8s.Volume(name="policy", config_map=k8s.ConfigMapVolumeSource(name=_POLICY_CONFIG_MAP))],
        containers=[
            k8s.Container(
                name=container_name,
                image=_IMAGE,
                image_pull_policy="Always",
                # `http://grocy` is the namespace-local Service name — it resolves to the
                # household's own grocy instance in whichever grocy-* namespace this runs in.
                args=["--policy", f"{_POLICY_DIR}/{_POLICY_FILE}", "--grocy-url", "http://grocy"],
                volume_mounts=[k8s.VolumeMount(name="policy", mount_path=_POLICY_DIR, read_only=True)],
                security_context=k8s.SecurityContext(
                    allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                ),
                resources=k8s.ResourceRequirements(
                    requests={"cpu": k8s.Quantity.from_string("25m"), "memory": k8s.Quantity.from_string("64Mi")},
                    limits={"memory": k8s.Quantity.from_string("128Mi")},
                ),
            )
        ],
    )


def base_chart(app: App) -> Chart:
    """The reconciler every household runs; the household overlay supplies the namespace."""
    chart = Chart(app, "grocy-user-perms", disable_resource_name_hashes=True)
    # The base `grocy-ingress` policy (app.py) restricts Grocy :80 to the Authentik outpost +
    # Gatus, because any pod that reaches :80 can impersonate any user via the trusted
    # X-authentik-username header. NetworkPolicies are additive, so this grants the
    # reconciler pods (bootstrap Job + daily CronJob) that same in-cluster access (they act
    # as the admin user to apply the policy). Scoped to the shared provisioner pod label only.
    k8s.KubeNetworkPolicy(
        chart,
        "networkpolicy",
        metadata=k8s.ObjectMeta(name="grocy-user-perms-provisioner-ingress"),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels={"app.kubernetes.io/name": "grocy"}),
            policy_types=["Ingress"],
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    from_=[k8s.NetworkPolicyPeer(pod_selector=k8s.LabelSelector(match_labels=_LABELS))],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(80), protocol="TCP")],
                )
            ],
        ),
    )
    k8s.KubeJob(
        chart,
        "job",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            # One-shot: runs at cluster bootstrap and again whenever the policy ConfigMap
            # (hash-suffixed by kustomize) or the image changes. The Job spec is immutable,
            # so let Flux recreate it.
            annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"},
        ),
        spec=k8s.JobSpec(
            backoff_limit=10,
            template=k8s.PodTemplateSpec(metadata=k8s.ObjectMeta(labels=_LABELS), spec=_pod_spec("provisioner")),
        ),
    )
    k8s.KubeCronJob(
        chart,
        "cronjob",
        metadata=k8s.ObjectMeta(
            name="grocy-user-perms-reconciler",
            annotations={
                "description": (
                    "Daily drift correction for the user→permission policy in policy.yaml. The one-shot"
                    " provisioner Job converges at bootstrap and on policy changes; this CronJob re-asserts"
                    " the same policy so manual permission edits in the Grocy UI (e.g. elevating haku)"
                    " converge back within a day."
                )
            },
        ),
        spec=k8s.CronJobSpec(
            schedule="40 4 * * *",
            concurrency_policy="Forbid",
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=3,
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    # A Job whose pod can never start (e.g. it pins a kustomize-hashed
                    # ConfigMap that a newer generation pruned) stays active forever --
                    # history limits and TTL apply only to finished Jobs. The deadline
                    # fails it; the TTL then garbage-collects it.
                    active_deadline_seconds=1800,
                    ttl_seconds_after_finished=86400,
                    backoff_limit=2,
                    template=k8s.PodTemplateSpec(metadata=k8s.ObjectMeta(labels=_LABELS), spec=_pod_spec("reconciler")),
                )
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    """The base, and each household's directory placing it in the household's namespace."""
    write_yaml(
        root / BASE_DIR / "kustomization.yaml",
        kustomize_kustomization(
            resources=[write_charts(root, BASE_DIR, base_chart)],
            components=[posixpath.relpath(PINS_DIR, BASE_DIR)],
            config_map_generator=[
                ConfigMapArgs(
                    name=_POLICY_CONFIG_MAP,
                    namespace=None,
                    literals=[f"{_POLICY_FILE}={yaml_config(_POLICY.model_dump())}"],
                )
            ],
        ),
    )
    for household in grocy.HOUSEHOLDS:
        directory = output_dir(household)
        (root / directory).mkdir(parents=True, exist_ok=True)
        write_yaml(
            root / directory / "kustomization.yaml",
            kustomize_kustomization(
                namespace=grocy.service(household).pods.namespace, resources=[posixpath.relpath(BASE_DIR, directory)]
            ),
        )
