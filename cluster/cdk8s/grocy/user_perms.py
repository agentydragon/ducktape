"""The Grocy user→permission reconciler shared by every household (`user-perms-base`): the
one-shot provisioner Job, the daily drift-correcting CronJob, and the NetworkPolicy
admitting both to Grocy.

Hand-written beside the generated output: `kustomization.yaml` (its configMapGenerator
hash-suffixes `policy.yaml`, so a policy edit changes the Job spec and the `force`
annotation makes Flux recreate it) and `image-pins/kustomization.yaml`, which overrides
the reconciler's "unset" placeholder tag (cluster/cdk8s/AGENTS.md § the `:tag` Setters
marker).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.forgejo_images import SECRET_NAME
from cluster.cdk8s.generation import write_charts

BASE_DIR = "cluster/k8s/grocy/user-perms-base"
_NAME = "grocy-user-perms-provisioner"
# Shared by the Job's and the CronJob's pods: the NetworkPolicy admits them to grocy:80.
_LABELS = {"app.kubernetes.io/name": _NAME}
# Script baked in via Bazel (//cluster/provisioners/grocy_user_perms:image).
_IMAGE = "git.allegedly.works/ducktape-ci/grocy-user-perms-provisioner:unset"
_POLICY_CONFIG_MAP = "grocy-user-perms-policy"


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
                args=["--policy", "/config/policy.yaml", "--grocy-url", "http://grocy"],
                volume_mounts=[k8s.VolumeMount(name="policy", mount_path="/config", read_only=True)],
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
    # as the admin user to apply policy.yaml). Scoped to the shared provisioner pod label only.
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
    write_charts(root, BASE_DIR, base_chart)
