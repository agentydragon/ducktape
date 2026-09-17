"""Everything ha-mcp needs, generated into one Kustomization: its Namespace, the
Job/CronJob/RBAC that validate and repair its Home Assistant long-lived token, and
the ConfigMap/Deployment/Service/CiliumNetworkPolicy/ServiceMonitor for the MCP
server itself. cdk8s generates all of it, so there's no real need to keep the
Namespace/credentials/app split into separate directories the way hand-written
manifests once did -- see cluster/docs/cdk8s.md.

Both the facade container's and the token-provisioner's image tags are deliberate
placeholders ("unset") -- image-pins/kustomization.yaml (hand-written, see
cluster/k8s/agents/ha-mcp/app/image-pins/kustomization.yaml) overrides them at
`kustomize build` time via Flux's image-automation marker. The ha-mcp container's own
image is pinned by digest directly and isn't Flux-managed.

bearer.sops.yaml (the facade's static bearer token) stays hand-written alongside this
generated output -- cdk8s has no key material to synthesize ciphertext with. See
cluster/docs/cdk8s.md § SOPS secrets in a converted directory.
"""

from __future__ import annotations

from cdk8s import ApiObject, ApiObjectMetadata, Cron, Duration, JsonPatch, Size
from cdk8s_plus_33 import (
    Capability,
    ConcurrencyPolicy,
    ConfigMap,
    ContainerPort,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    CronJob,
    Deployment,
    EnvFrom,
    EnvValue,
    ImagePullPolicy,
    ISecret,
    Job,
    MemoryResources,
    Namespace,
    Probe,
    Protocol,
    RestartPolicy,
    Role,
    RoleBinding,
    Secret,
    SecretValue,
    Service,
    ServiceAccount,
    ServicePort,
    Volume,
)
from cilium_crds.io.cilium import (
    CiliumNetworkPolicy,
    CiliumNetworkPolicySpec,
    CiliumNetworkPolicySpecEndpointSelector,
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEndpoints,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecSelector,
)

from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret

_NAMESPACE = "ha-mcp"
_HOME_ASSISTANT_NAMESPACE = "home-assistant"  # where the token-provisioner's SA/Job/CronJob run
_HOME_ASSISTANT_TOKEN_SECRET_NAME = "ha-mcp-home-assistant-token"
_PLACEHOLDER_TAG = "unset"

_PROVISIONER_NAME = "ha-mcp-token-provisioner"
_PROVISIONER_IMAGE_NAME = "git.allegedly.works/ducktape-ci/ha-mcp-token-provisioner"
_PROVISIONER_LABELS = {"app.kubernetes.io/name": _PROVISIONER_NAME}

_APP_NAME = "ha-mcp"
_APP_FACADE_IMAGE_NAME = "git.allegedly.works/ducktape-ci/mcp-oauth-facade"
_APP_CONFIG_MAP_NAME = "ha-mcp-config"
_APP_UPSTREAM_PORT = 8086
_APP_FACADE_PORT = 8765
_APP_METRICS_PORT = 9090
_APP_LABELS = {"app.kubernetes.io/name": _APP_NAME}


def _metadata(
    name: str, namespace: str, *, labels: dict[str, str] | None = None, annotations: dict[str, str] | None = None
) -> ApiObjectMetadata:
    return ApiObjectMetadata(name=name, namespace=namespace, labels=labels, annotations=annotations)


class HaMcpCredentialsProvisioner(Construct):
    """Validates and repairs the token after expiry, revocation, or a Home Assistant restore."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=_HOME_ASSISTANT_NAMESPACE)
        service_account = ServiceAccount(
            self, "serviceaccount", metadata=_metadata(_PROVISIONER_NAME, _HOME_ASSISTANT_NAMESPACE)
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
        role = Role(self, "role", metadata=_metadata(_PROVISIONER_NAME, _NAMESPACE))
        ApiObject.of(role).add_json_patch(
            JsonPatch.add(
                "/rules",
                [
                    {
                        "apiGroups": [""],
                        "resources": ["secrets"],
                        "resourceNames": [_HOME_ASSISTANT_TOKEN_SECRET_NAME],
                        "verbs": ["get", "update", "patch"],
                    },
                    {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
                ],
            )
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=_metadata(_PROVISIONER_NAME, _NAMESPACE),
            role=Role.from_role_name(self, "role-ref", _PROVISIONER_NAME),
        ).add_subjects(service_account)

    def _add_container(self, workload: Job | CronJob, break_glass_secret: ISecret) -> None:
        workload.add_container(
            name="provision-token",
            image=f"{_PROVISIONER_IMAGE_NAME}:{_PLACEHOLDER_TAG}",
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
            metadata=_metadata(
                _PROVISIONER_NAME,
                _HOME_ASSISTANT_NAMESPACE,
                annotations={
                    "description": "Validates and repairs the dedicated Home Assistant long-lived token consumed by HA-MCP.",
                    # Idempotent repair loop -- it validates the token and only rewrites it
                    # when broken -- so re-running on a cadence is the intended behaviour,
                    # not a side effect. That makes the TTL safe here, and the TTL is what
                    # lets a failed run recover: Flux's `wait: true` blocks on every object
                    # it applies, so a Failed Job holds this Kustomization (and the rest of
                    # ha-mcp behind it) unready forever. Job specs are immutable, so
                    # re-applying an unchanged manifest is a no-op, and this `force`
                    # annotation only fires on a manifest change -- neither retries after an
                    # *environmental* failure. The TTL deletes the finished Job; the next
                    # reconcile recreates and re-runs it.
                    #
                    # Deliberately NOT applied to change-driven provisioners (the
                    # readonly-role GRANT Jobs): for those the TTL would convert a
                    # run-on-change script into a run-on-schedule one. See
                    # study-casino/db/readonly-role-provisioner-job.yaml.
                    "kustomize.toolkit.fluxcd.io/force": "enabled",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_PROVISIONER_LABELS),
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
            metadata=_metadata(_PROVISIONER_NAME, _HOME_ASSISTANT_NAMESPACE),
            pod_metadata=ApiObjectMetadata(labels=_PROVISIONER_LABELS),
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


class HaMcpApp(Construct):
    """The ha-mcp Deployment/Service/ConfigMap/NetworkPolicy/ServiceMonitor and its pull credentials."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=_NAMESPACE)
        config_map = self._add_config_map()
        deployment = self._add_deployment(config_map)
        self._add_service(deployment)
        self._add_network_policy()
        self._add_service_monitor()

    def _add_config_map(self) -> ConfigMap:
        return ConfigMap(
            self,
            "config",
            metadata=_metadata(_APP_CONFIG_MAP_NAME, _NAMESPACE),
            data={
                "HOMEASSISTANT_URL": "http://home-assistant.home-assistant.svc.cluster.local:8123",
                "MCP_HOST": "0.0.0.0",
                "MCP_PORT": str(_APP_UPSTREAM_PORT),
                "MCP_SECRET_PATH": "/mcp",
                "MCP_HEALTHZ": "true",
                "MCP_SERVER_NAME": "Home Assistant",
                "ENVIRONMENT": "production",
                "LOG_LEVEL": "INFO",
                "BACKUP_HINT": "normal",
                "HA_MCP_CONFIG_DIR": "/data",
                "HAMCP_BACKUP_DIR": "/data/backups",
                "ENABLE_TOOL_SEARCH": "false",
                "READ_ONLY_MODE": "false",
                "ENABLE_BETA_FEATURES": "false",
                "ENABLE_CODE_MODE": "false",
                "HAMCP_ENABLE_FILESYSTEM_TOOLS": "false",
                "ENABLE_YAML_CONFIG_EDITING": "false",
                "HAMCP_ENABLE_DEV_MODE": "false",
                "ENABLE_TOOL_SECURITY_POLICIES": "false",
                "MCP_FACADE_FACADE_NAME": "Home Assistant MCP Facade",
                "MCP_FACADE_UPSTREAM__KIND": "http",
                "MCP_FACADE_UPSTREAM__URL": f"http://localhost:{_APP_UPSTREAM_PORT}/mcp",
                # Client auth is a cluster-internal static bearer (MCP_FACADE_CLIENT_AUTH__STATIC_BEARER,
                # injected from the ha-mcp-bearer Secret below), not the public Authentik OAuth gate.
                # FacadeSettings requires exactly one of `auth` / `client_auth`, so the MCP_FACADE_AUTH__*
                # keys are absent by design. With no OAuth there is no client registration or token state
                # to keep, so the facade needs no persistence backend and the Valkey is gone with it.
            },
        )

    def _add_deployment(self, config_map: ConfigMap) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=_metadata(
                _APP_NAME,
                _NAMESPACE,
                labels=_APP_LABELS,
                annotations={
                    "description": (
                        "Writable Home Assistant MCP server behind the shared MCP facade, cluster-internal "
                        "and gated by a static bearer that only haku-console holds. The upstream HA token "
                        "remains server-side, and Haku applies its own per-call approval policy."
                    ),
                    "reloader.stakater.com/auto": "true",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_APP_LABELS),
            replicas=1,
            docker_registry_auth=Secret.from_secret_name(self, "forgejo-images-creds-ref", "forgejo-images-creds"),
            automount_service_account_token=False,
        )

        tmp_volume = Volume.from_empty_dir(self, "tmp-volume", "tmp")
        data_volume = Volume.from_empty_dir(self, "data-volume", "data")

        deployment.add_container(
            name="ha-mcp",
            image="ghcr.io/homeassistant-ai/ha-mcp:8.4.3@sha256:d5cea47a0115e5d161c2b319ee637b1b0a5bcfafe1597cb490299bbbc6329456",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=["ha-mcp-web"],
            ports=[ContainerPort(name="upstream", number=_APP_UPSTREAM_PORT, protocol=Protocol.TCP)],
            env_from=[EnvFrom(config_map=config_map)],
            env_variables={
                "HOMEASSISTANT_TOKEN": EnvValue.from_secret_value(
                    SecretValue(
                        secret=Secret.from_secret_name(
                            self, "ha-mcp-home-assistant-token-ref", _HOME_ASSISTANT_TOKEN_SECRET_NAME
                        ),
                        key="token",
                    )
                )
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50), limit=Cpu.millis(500)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=Size.gibibytes(1)),
            ),
            readiness=Probe.from_http_get(
                "/healthz",
                port=_APP_UPSTREAM_PORT,
                initial_delay_seconds=Duration.seconds(5),
                period_seconds=Duration.seconds(10),
            ),
            liveness=Probe.from_http_get(
                "/healthz",
                port=_APP_UPSTREAM_PORT,
                initial_delay_seconds=Duration.seconds(20),
                period_seconds=Duration.seconds(20),
            ),
            security_context=ContainerSecurityContextProps(
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]), user=999, group=999
            ),
        )
        deployment.containers[0].mount("/tmp", tmp_volume)
        deployment.containers[0].mount("/data", data_volume)

        deployment.add_container(
            name="facade",
            image=f"{_APP_FACADE_IMAGE_NAME}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            ports=[
                ContainerPort(name="http", number=_APP_FACADE_PORT, protocol=Protocol.TCP),
                ContainerPort(name="metrics", number=_APP_METRICS_PORT, protocol=Protocol.TCP),
            ],
            env_from=[EnvFrom(config_map=config_map)],
            env_variables={
                # The same token haku-console presents (reflected as ha-mcp-bearer into
                # haku-console by the emberstack reflector) -- one source of truth, no drift.
                "MCP_FACADE_CLIENT_AUTH__STATIC_BEARER": EnvValue.from_secret_value(
                    SecretValue(
                        secret=Secret.from_secret_name(self, "ha-mcp-bearer-ref", "ha-mcp-bearer"), key="bearer-token"
                    )
                )
            },
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50), limit=Cpu.millis(200)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(256)),
            ),
            readiness=Probe.from_http_get(
                "/healthz",
                port=_APP_FACADE_PORT,
                initial_delay_seconds=Duration.seconds(5),
                period_seconds=Duration.seconds(10),
            ),
            liveness=Probe.from_http_get(
                "/healthz",
                port=_APP_FACADE_PORT,
                initial_delay_seconds=Duration.seconds(20),
                period_seconds=Duration.seconds(20),
            ),
            # cdk8s_plus_33 defaults containers to a hardened SecurityContext
            # (readOnlyRootFilesystem/runAsNonRoot: true). Opt out explicitly to preserve
            # today's actual (unrestricted) behavior -- the real container's
            # filesystem-write/root needs were never audited, so silently hardening it here
            # could break the running facade. Same rationale as litellm_constructs.py.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False, ensure_non_root=False),
        )
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=_metadata(_APP_NAME, _NAMESPACE, labels=_APP_LABELS),
            selector=deployment,
            ports=[
                ServicePort(name="http", port=_APP_FACADE_PORT, target_port=_APP_FACADE_PORT, protocol=Protocol.TCP),
                ServicePort(
                    name="metrics", port=_APP_METRICS_PORT, target_port=_APP_METRICS_PORT, protocol=Protocol.TCP
                ),
            ],
        )

    def _ingress_rule(self, source_namespace: str, port: int) -> CiliumNetworkPolicySpecIngress:
        return CiliumNetworkPolicySpecIngress(
            from_endpoints=[
                CiliumNetworkPolicySpecIngressFromEndpoints(
                    match_labels={"k8s:io.kubernetes.pod.namespace": source_namespace}
                )
            ],
            to_ports=[
                CiliumNetworkPolicySpecIngressToPorts(
                    ports=[
                        CiliumNetworkPolicySpecIngressToPortsPorts(
                            port=str(port), protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP
                        )
                    ]
                )
            ],
        )

    def _add_network_policy(self) -> None:
        CiliumNetworkPolicy(
            self,
            "networkpolicy",
            metadata=_metadata(
                "ha-mcp-ingress",
                _NAMESPACE,
                annotations={
                    "description": (
                        "Default-deny ingress for HA-MCP. Only haku-console reaches the facade port; the "
                        "upstream server port is reachable only over pod-local loopback. No Gateway ingress "
                        "-- this MCP is cluster-internal since the move to a static bearer."
                    )
                },
            ),
            spec=CiliumNetworkPolicySpec(
                endpoint_selector=CiliumNetworkPolicySpecEndpointSelector(match_labels=_APP_LABELS),
                ingress=[
                    self._ingress_rule("haku-console", _APP_FACADE_PORT),
                    self._ingress_rule("monitoring", _APP_METRICS_PORT),
                ],
            ),
        )

    def _add_service_monitor(self) -> None:
        ServiceMonitor(
            self,
            "servicemonitor",
            metadata=_metadata(_APP_NAME, _NAMESPACE),
            spec=ServiceMonitorSpec(
                selector=ServiceMonitorSpecSelector(match_labels=_APP_LABELS),
                endpoints=[ServiceMonitorSpecEndpoints(port="metrics", interval="30s")],
            ),
        )


class HaMcp(Construct):
    """The whole ha-mcp Kustomization: its Namespace, the app, and the Job/CronJob
    that keep its Home Assistant token valid."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        Namespace(
            self,
            "namespace",
            metadata=ApiObjectMetadata(
                name=_NAMESPACE,
                labels={"app.kubernetes.io/name": _NAMESPACE, "goldilocks.fairwinds.com/enabled": "false"},
            ),
        )
        HaMcpCredentialsProvisioner(self, "provisioner")
        HaMcpApp(self, "app")
