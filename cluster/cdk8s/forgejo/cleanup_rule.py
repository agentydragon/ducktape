"""Reconcile a disabled native Forgejo container-package cleanup rule."""

from __future__ import annotations

import json
from dataclasses import asdict

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on,
)
from cluster.cdk8s.forgejo_registry.chart import SECRET_NAME
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from skills.forgejo.scripts.cleanup_rule import Rule

NAME = "forgejo-cleanup-rule"
NAMESPACE = "forgejo-images"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}-image-pins"
IMAGE = f"git.allegedly.works/ducktape-ci/{NAME}:unset"

RULE = Rule(
    enabled=False,
    type="container",
    keep_count=10,
    keep_pattern="",
    remove_days=30,
    remove_pattern=r"^devel-[0-9]{14}-[0-9a-f]{7}$",
    match_full_name=False,
)
CONFIG_MAP = ConfigMapArgs(
    name="forgejo-cleanup-rule-config",
    namespace=NAMESPACE,
    literals=["rule.json=" + json.dumps(asdict(RULE), sort_keys=True)],
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeCronJob(
        chart,
        "job",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE),
        spec=k8s.CronJobSpec(
            schedule="20 8 * * *",
            concurrency_policy="Forbid",
            successful_jobs_history_limit=3,
            failed_jobs_history_limit=3,
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    active_deadline_seconds=300,
                    backoff_limit=1,
                    ttl_seconds_after_finished=604800,
                    template=k8s.PodTemplateSpec(
                        spec=k8s.PodSpec(
                            automount_service_account_token=False,
                            image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                            restart_policy="Never",
                            security_context=k8s.PodSecurityContext(
                                run_as_non_root=True,
                                run_as_user=65534,
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                            volumes=[
                                k8s.Volume(name="rule", config_map=k8s.ConfigMapVolumeSource(name=CONFIG_MAP.name))
                            ],
                            containers=[
                                k8s.Container(
                                    name="reconcile",
                                    image=IMAGE,
                                    args=["--config", "/config/rule.json"],
                                    env=[
                                        k8s.EnvVar(
                                            name="FORGEJO_USER",
                                            value_from=k8s.EnvVarSource(
                                                secret_key_ref=k8s.SecretKeySelector(name=SECRET_NAME, key="username")
                                            ),
                                        ),
                                        k8s.EnvVar(
                                            name="FORGEJO_PASSWORD",
                                            value_from=k8s.EnvVarSource(
                                                secret_key_ref=k8s.SecretKeySelector(name=SECRET_NAME, key="password")
                                            ),
                                        ),
                                    ],
                                    volume_mounts=[k8s.VolumeMount(name="rule", mount_path="/config", read_only=True)],
                                    security_context=k8s.SecurityContext(
                                        allow_privilege_escalation=False,
                                        capabilities=k8s.Capabilities(drop=["ALL"]),
                                        read_only_root_filesystem=True,
                                    ),
                                    resources=k8s.ResourceRequirements(
                                        requests={
                                            "cpu": k8s.Quantity.from_string("25m"),
                                            "memory": k8s.Quantity.from_string("64Mi"),
                                        },
                                        limits={"memory": k8s.Quantity.from_string("128Mi")},
                                    ),
                                )
                            ],
                        )
                    ),
                )
            ),
        ),
    )
    return chart


def cleanup_rule(chart: Chart, directory: RenderedDirectory, forgejo_images: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, NAME, directory, timeout="5m", depends_on=[flux_kustomization_depends_on(forgejo_images)]
    )
