"""Job + CronJob that validate and repair ha-mcp's Home Assistant long-lived
token, plus the RBAC and pull credentials they need.

The image tag is a deliberate placeholder ("unset") -- image-pins/kustomization.yaml
(hand-written, see cluster/k8s/agents/ha-mcp/credentials/image-pins/kustomization.yaml)
overrides it at `kustomize build` time via Flux's image-automation marker. See
cluster/docs/cdk8s.md.
"""

from __future__ import annotations

from cdk8s import ApiObject, ApiObjectMetadata, Cron, Duration, JsonPatch, Size
from cdk8s_plus_33 import (
    Capability,
    ConcurrencyPolicy,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    CronJob,
    EnvValue,
    ImagePullPolicy,
    ISecret,
    Job,
    MemoryResources,
    RestartPolicy,
    Role,
    RoleBinding,
    Secret,
    SecretValue,
    ServiceAccount,
)
from constructs import Construct

from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret

_NAME = "ha-mcp-token-provisioner"
_SA_NAMESPACE = "home-assistant"
_RBAC_NAMESPACE = "ha-mcp"
_SECRET_NAME = "ha-mcp-home-assistant-token"
_IMAGE_NAME = "git.allegedly.works/ducktape-ci/ha-mcp-token-provisioner"
_PLACEHOLDER_TAG = "unset"


def _pod_labels() -> dict[str, str]:
    return {"app.kubernetes.io/name": _NAME}


class HaMcpCredentialsProvisioner(Construct):
    """Validates and repairs the token after expiry, revocation, or a Home Assistant restore."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=_SA_NAMESPACE)
        service_account = ServiceAccount(
            self, "serviceaccount", metadata=ApiObjectMetadata(name=_NAME, namespace=_SA_NAMESPACE)
        )
        self._add_rbac(service_account)
        break_glass_secret = Secret.from_secret_name(self, "home-assistant-break-glass", "home-assistant-break-glass")
        pull_secret = Secret.from_secret_name(self, "forgejo-images-creds-ref", "forgejo-images-creds")
        self._add_job(service_account, break_glass_secret, pull_secret)
        self._add_cronjob(service_account, break_glass_secret, pull_secret)

    def _add_rbac(self, service_account: ServiceAccount) -> None:
        # cdk8s_plus_33's RolePolicyRule has no resourceNames field, so this Role's
        # /rules (which needs one) is patched in directly -- same escape hatch as
        # Deployment's topologySpreadConstraints in litellm_constructs.py: the typed
        # Role construct stays authoritative for apiVersion/kind/metadata, and
        # ApiObject.of() reaches its internally-managed ApiObject for the one field
        # the typed API can't express.
        role = Role(self, "role", metadata=ApiObjectMetadata(name=_NAME, namespace=_RBAC_NAMESPACE))
        ApiObject.of(role).add_json_patch(
            JsonPatch.add(
                "/rules",
                [
                    {
                        "apiGroups": [""],
                        "resources": ["secrets"],
                        "resourceNames": [_SECRET_NAME],
                        "verbs": ["get", "update", "patch"],
                    },
                    {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
                ],
            )
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name=_NAME, namespace=_RBAC_NAMESPACE),
            role=Role.from_role_name(self, "role-ref", _NAME),
        ).add_subjects(service_account)

    def _add_container(self, workload: Job | CronJob, break_glass_secret: ISecret) -> None:
        workload.add_container(
            name="provision-token",
            image=f"{_IMAGE_NAME}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            env_variables={
                "HOME_ASSISTANT_LOCAL_ADMIN_PASSWORD": EnvValue.from_secret_value(
                    SecretValue(secret=break_glass_secret, key="password")
                )
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(20)),
                memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
            ),
            security_context=ContainerSecurityContextProps(
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
                # Unlike litellm_constructs.py's Deployment, no override needed here:
                # cdk8s_plus_33's hardened ensure_non_root default (true) already
                # matches this container's real requirement (runAsNonRoot: true).
                #
                # readOnlyRootFilesystem stays permissive (false) to preserve the
                # original manifest's behavior -- the container's actual
                # filesystem-write needs haven't been audited.
                read_only_root_filesystem=False,
            ),
        )

    def _add_job(self, service_account: ServiceAccount, break_glass_secret: ISecret, pull_secret: ISecret) -> None:
        job = Job(
            self,
            "job",
            metadata=ApiObjectMetadata(
                name=_NAME,
                namespace=_SA_NAMESPACE,
                annotations={
                    "description": "Validates and repairs the dedicated Home Assistant long-lived token consumed by HA-MCP.",
                    # Idempotent repair loop -- it validates the token and only rewrites it
                    # when broken -- so re-running on a cadence is the intended behaviour,
                    # not a side effect. That makes the TTL safe here, and the TTL is what
                    # lets a failed run recover: Flux's `wait: true` blocks on every object
                    # it applies, so a Failed Job holds this Kustomization (and ha-mcp +
                    # ha-mcp-servicemonitor behind it) unready forever. Job specs are
                    # immutable, so re-applying an unchanged manifest is a no-op, and this
                    # `force` annotation only fires on a manifest change -- neither retries
                    # after an *environmental* failure. The TTL deletes the finished Job;
                    # the next reconcile recreates and re-runs it.
                    #
                    # Deliberately NOT applied to change-driven provisioners (the
                    # readonly-role GRANT Jobs): for those the TTL would convert a
                    # run-on-change script into a run-on-schedule one. See
                    # study-casino/db/readonly-role-provisioner-job.yaml.
                    "kustomize.toolkit.fluxcd.io/force": "enabled",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_pod_labels()),
            backoff_limit=3,
            ttl_after_finished=Duration.seconds(3600),
            restart_policy=RestartPolicy.ON_FAILURE,
            service_account=service_account,
            docker_registry_auth=pull_secret,
            # cdk8s_plus_33 defaults pods to no mounted SA token. This container calls the
            # K8s API (via the RBAC role above) to patch its own Secret, so it needs one.
            automount_service_account_token=True,
        )
        self._add_container(job, break_glass_secret)

    def _add_cronjob(self, service_account: ServiceAccount, break_glass_secret: ISecret, pull_secret: ISecret) -> None:
        cronjob = CronJob(
            self,
            "cronjob",
            metadata=ApiObjectMetadata(name=_NAME, namespace=_SA_NAMESPACE),
            pod_metadata=ApiObjectMetadata(labels=_pod_labels()),
            # Weekly, Sunday 04:00 -- matches the Job's own weekly-repair cadence.
            schedule=Cron.schedule(minute="0", hour="4", week_day="0"),
            concurrency_policy=ConcurrencyPolicy.FORBID,
            successful_jobs_retained=3,
            failed_jobs_retained=3,
            backoff_limit=3,
            restart_policy=RestartPolicy.ON_FAILURE,
            service_account=service_account,
            docker_registry_auth=pull_secret,
            automount_service_account_token=True,
        )
        self._add_container(cronjob, break_glass_secret)
