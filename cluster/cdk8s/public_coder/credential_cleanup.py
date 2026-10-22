"""One-shot cleanup of Public Coder Secrets whose source controllers retain their targets."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "public-coder-agent-secret-cleanup"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/public-coder-agent/secret-cleanup"
NAMESPACE = "ducktape-flux"
TARGET_NAMESPACE = "public-coder-agent"
_SECRETS = (
    "aiquota-api-bearer-public-coder",
    "brave-search-api-key",
    "buildbuddy-api-key",
    "clickhouse-public-coder-credentials",
    "haku-console-public-coder-agent",
    "public-coder-agent-devbox-ssh-key",
    "public-coder-agent-github-token",
    "public-coder-agent-matrix-bot-password",
    "public-coder-agent-proxy-ca",
    "public-coder-agent-sshpiper-devbox-key",
    "public-coder-agent-sshpiper-host-key",
    "public-coder-devbox-ssh-host-key",
)
_KUBECTL_IMAGE = "bitnamilegacy/kubectl:1.33.4@sha256:ed0b31a0508da84ee655c5c6e01bd3897fc56ad6cf69debb27fa1893a06d2246"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeServiceAccount(chart, "service-account", metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE))
    k8s.KubeRole(
        chart,
        "target-secret-deleter",
        metadata=k8s.ObjectMeta(name=NAME, namespace=TARGET_NAMESPACE),
        rules=[k8s.PolicyRule(api_groups=[""], resources=["secrets"], resource_names=list(_SECRETS), verbs=["delete"])],
    )
    k8s.KubeRoleBinding(
        chart,
        "target-secret-deleter-binding",
        metadata=k8s.ObjectMeta(name=NAME, namespace=TARGET_NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=NAME),
        subjects=[k8s.Subject(kind="ServiceAccount", name=NAME, namespace=NAMESPACE)],
    )
    k8s.KubeJob(
        chart,
        "job",
        metadata=k8s.ObjectMeta(
            name=NAME, namespace=NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"}
        ),
        spec=k8s.JobSpec(
            active_deadline_seconds=300,
            backoff_limit=3,
            template=k8s.PodTemplateSpec(
                spec=k8s.PodSpec(
                    service_account_name=NAME,
                    automount_service_account_token=True,
                    restart_policy="Never",
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=65532,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        k8s.Container(
                            name="delete-retired-secrets",
                            image=_KUBECTL_IMAGE,
                            image_pull_policy="IfNotPresent",
                            command=["/bin/sh", "-ec"],
                            args=[
                                "kubectl -n public-coder-agent delete secret --ignore-not-found=true "
                                + " ".join(_SECRETS)
                            ],
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                                run_as_non_root=True,
                                run_as_user=65532,
                            ),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("32Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("100m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                            ),
                        )
                    ],
                )
            ),
        ),
    )
    return chart
