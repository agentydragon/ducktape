"""Reusable cdk8s constructs for the Agentplane staging/testing environments' app/
directory: the integration app Deployment (+ Alembic migrate initContainer), its own
RBAC, HTTPRoute, NetworkPolicy, the runner SandboxTemplate, and the three
ServiceAccounts (agent/app/runner) involved.

The app Deployment's image tags are deliberate placeholders ("unset") -- the sibling
image-pins/ Kustomize Component (hand-written, never generated) overrides them at
`kustomize build` time via Flux's image-automation marker. See cluster/docs/cdk8s.md.
The runner SandboxTemplate's two container images carry the same placeholder for the
same reason -- kustomize's `images:` transformer patches by image name across every
resource in a Kustomization, not just Deployments, so the same image-pins/
Component also covers these.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

from agent_sandbox_sandboxtemplate_crds.io.x_k8s.agents.extensions import (
    SandboxTemplate,
    SandboxTemplateSpec,
    SandboxTemplateSpecNetworkPolicyManagement,
    SandboxTemplateSpecPodTemplate,
    SandboxTemplateSpecPodTemplateMetadata,
    SandboxTemplateSpecPodTemplateSpec,
    SandboxTemplateSpecPodTemplateSpecContainers,
    SandboxTemplateSpecPodTemplateSpecContainersEnv,
    SandboxTemplateSpecPodTemplateSpecContainersPorts,
    SandboxTemplateSpecPodTemplateSpecContainersResources,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests,
    SandboxTemplateSpecPodTemplateSpecContainersSecurityContext,
    SandboxTemplateSpecPodTemplateSpecContainersSecurityContextCapabilities,
    SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts,
    SandboxTemplateSpecPodTemplateSpecImagePullSecrets,
    SandboxTemplateSpecPodTemplateSpecSecurityContext,
    SandboxTemplateSpecPodTemplateSpecSecurityContextSeccompProfile,
    SandboxTemplateSpecPodTemplateSpecVolumes,
    SandboxTemplateSpecPodTemplateSpecVolumesConfigMap,
    SandboxTemplateSpecPodTemplateSpecVolumesProjected,
    SandboxTemplateSpecPodTemplateSpecVolumesProjectedSources,
    SandboxTemplateSpecPodTemplateSpecVolumesProjectedSourcesServiceAccountToken,
    SandboxTemplateSpecVolumeClaimTemplates,
    SandboxTemplateSpecVolumeClaimTemplatesMetadata,
    SandboxTemplateSpecVolumeClaimTemplatesPolicy,
    SandboxTemplateSpecVolumeClaimTemplatesSpec,
    SandboxTemplateSpecVolumeClaimTemplatesSpecResources,
    SandboxTemplateSpecVolumeClaimTemplatesSpecResourcesRequests,
)
from cdk8s import ApiObject, ApiObjectMetadata, Duration, JsonPatch, Size
from cdk8s_plus_34 import (
    ApiResource,
    Capability,
    ConfigMap,
    ContainerPort,
    ContainerProps,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    IApiResource,
    ImagePullPolicy,
    MemoryResources,
    Node,
    NodeLabelQuery,
    PodSecurityContextProps,
    Protocol,
    Role,
    RoleBinding,
    RolePolicyRule,
    Secret,
    SecretValue,
    Service,
    ServiceAccount,
    ServicePort,
    Volume,
    k8s,
)
from cilium_crds.io.cilium import (
    CiliumNetworkPolicy,
    CiliumNetworkPolicySpec,
    CiliumNetworkPolicySpecEgress,
    CiliumNetworkPolicySpecEgressToEndpoints,
    CiliumNetworkPolicySpecEgressToEntities,
    CiliumNetworkPolicySpecEgressToPorts,
    CiliumNetworkPolicySpecEgressToPortsPorts,
    CiliumNetworkPolicySpecEgressToPortsPortsProtocol,
    CiliumNetworkPolicySpecEndpointSelector,
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEndpoints,
    CiliumNetworkPolicySpecIngressFromEntities,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)
from constructs import Construct
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecParentRefs,
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
    HttpRouteSpecRulesFilters,
    HttpRouteSpecRulesFiltersResponseHeaderModifier,
    HttpRouteSpecRulesFiltersResponseHeaderModifierSet,
    HttpRouteSpecRulesFiltersType,
    HttpRouteSpecRulesTimeouts,
)

from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_NAME = "agentplane-app"
_APP_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-app"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-app-migrate"
_RUNNER_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner"
_EGRESS_SIDECAR_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar"
_CONTAINER_PORT = 8080
_ZONE = "hil-ovh"
_LABELS = {"app.kubernetes.io/name": _NAME}
_RUNNER_LABELS = {"app.kubernetes.io/name": "agentplane-runner"}


# cdk8s_plus_34's Python stub doesn't declare ApiResource as implementing
# IApiResource's `resource_name` member (see agentplane_constructs.py's `_custom`,
# same cast for the same reason).
def _custom(api_group: str, resource_type: str) -> IApiResource:
    return cast(IApiResource, ApiResource.custom(api_group=api_group, resource_type=resource_type))


@dataclass(frozen=True)
class AgentplaneAppEnvSpec:
    """Per-environment values for the integration app and its runner SandboxTemplate."""

    namespace: str
    replicas: int
    strategy: DeploymentStrategy
    min_ready_seconds: int | None
    # staging spreads its 2 replicas across nodes; testing's single replica has
    # nothing to spread.
    topology_spread: bool
    enable_pdb: bool
    hostname: str
    oidc_issuer: str
    # staging's OIDC provider is the in-cluster Authentik Service, reached both by its
    # public hostname and directly; testing's Dex is reached only by its public
    # hostname (both environments' egress rule to that public hostname is unconditional).
    reach_incluster_authentik: bool
    # staging pins runner Pods to be near the database/LiteLLM; testing has no pin.
    runner_zone: str | None
    # The interception CA ConfigMap the egress Bundle (Stage 3) publishes -- same
    # asymmetric (unprefixed staging / namespace-prefixed testing) name threaded
    # through AgentplaneEgressEnvSpec.ca_secret_name.
    runner_ca_configmap_name: str


class AgentplaneApp(Construct):
    """ServiceAccounts, RBAC, Deployment (+ migrate initContainer), Service,
    HTTPRoute, NetworkPolicy, optional PodDisruptionBudget, and the runner
    SandboxTemplate.
    """

    def __init__(self, scope: Construct, id: str, spec: AgentplaneAppEnvSpec) -> None:
        super().__init__(scope, id)
        self.spec = spec

        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=spec.namespace)
        app_service_account = self._add_service_accounts()
        self._add_rbac(app_service_account)
        deployment = self._add_deployment(app_service_account)
        self._add_service(deployment)
        self._add_http_route()
        self._add_network_policy()
        if spec.enable_pdb:
            self._add_pdb()
        self._add_sandbox_template()

    def _add_service_accounts(self) -> ServiceAccount:
        namespace = self.spec.namespace
        # The identity an agent presents to the app's own API -- no Pod runs as it, so
        # it needs no mounted token (see serviceaccount-agentplane-agent.yaml's own
        # comment, preserved in the generated file).
        ServiceAccount(
            self, "serviceaccount-agent", metadata=metadata("agentplane-agent", namespace), automount_token=False
        )
        # cdk8s_plus_34 defaults ServiceAccounts to automount_token=False; the app
        # mounts its own token to call TokenReview as itself.
        app_service_account = ServiceAccount(
            self, "serviceaccount-app", metadata=metadata(_NAME, namespace), automount_token=True
        )
        # The runner Pods' identity, with no RBAC of its own -- see
        # serviceaccount-agentplane-runner.yaml's own comment.
        ServiceAccount(
            self, "serviceaccount-runner", metadata=metadata("agentplane-runner", namespace), automount_token=False
        )
        return app_service_account

    def _add_rbac(self, app_service_account: ServiceAccount) -> None:
        namespace = self.spec.namespace
        # TokenReview proves a Bearer token the app itself was handed. Creating a
        # review grants none of the reviewed identity's authority.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-app-token-reviewer",
            service_account_name=_NAME,
            namespace=namespace,
        )
        Role(
            self,
            "role",
            metadata=metadata(_NAME, namespace),
            rules=[
                RolePolicyRule(
                    resources=[_custom("extensions.agents.x-k8s.io", "sandboxtemplates")], verbs=["get", "list"]
                ),
                RolePolicyRule(
                    resources=[_custom("agents.x-k8s.io", "sandboxes")],
                    verbs=["create", "get", "list", "watch", "patch", "delete"],
                ),
                RolePolicyRule(resources=[cast(IApiResource, ApiResource.PODS)], verbs=["get", "list", "watch"]),
                # One ServiceAccount per Sandbox, created with it and owned by it; no
                # patching beyond stamping that owner reference, and no reading of the
                # tokens minted for it.
                RolePolicyRule(resources=[_custom("", "serviceaccounts")], verbs=["create", "patch", "delete"]),
                RolePolicyRule(
                    resources=[
                        _custom("agentplane.allegedly.works", resource)
                        for resource in ("egresspolicies", "egressbindings", "egresscredentials")
                    ],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(
                    resources=[_custom("agentplane.allegedly.works", "egressbindings")], verbs=["create", "delete"]
                ),
                RolePolicyRule(
                    resources=[
                        _custom("agentplane.allegedly.works", resource)
                        for resource in ("actionpolicysets", "actionpolicybindings")
                    ],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(
                    resources=[_custom("agentplane.allegedly.works", "actionpolicybindings")],
                    verbs=["create", "delete"],
                ),
            ],
        )
        RoleBinding(
            self, "rolebinding", metadata=metadata(_NAME, namespace), role=Role.from_role_name(self, "role-ref", _NAME)
        ).add_subjects(app_service_account)

    def _container_env(self) -> dict[str, EnvValue]:
        namespace = self.spec.namespace
        action_federation = ConfigMap.from_config_map_name(
            self, "action-federation-config-ref", "agentplane-action-federation"
        )
        postgres_app = Secret.from_secret_name(self, "postgres-app-secret", "postgres-app")
        oidc_secret = Secret.from_secret_name(self, "agentplane-oidc-secret", "agentplane-oidc")
        token_subjects = json.dumps([f"system:serviceaccount:{namespace}:agentplane-agent"])
        return {
            "AGENTPLANE_ACTION_FEDERATION": EnvValue.from_config_map(action_federation, "action-federation"),
            "AGENTPLANE_CONFIG_FILE": EnvValue.from_value("/etc/agentplane/config.yaml"),
            "AGENTPLANE_DB_USER": EnvValue.from_secret_value(SecretValue(secret=postgres_app, key="username")),
            "AGENTPLANE_DB_PASSWORD": EnvValue.from_secret_value(SecretValue(secret=postgres_app, key="password")),
            "AGENTPLANE_DB_HOST": EnvValue.from_secret_value(SecretValue(secret=postgres_app, key="host")),
            "AGENTPLANE_DB_PORT": EnvValue.from_secret_value(SecretValue(secret=postgres_app, key="port")),
            "AGENTPLANE_DB_NAME": EnvValue.from_secret_value(SecretValue(secret=postgres_app, key="dbname")),
            "AGENTPLANE_DATABASE_URL": EnvValue.from_value(
                "postgresql+asyncpg://$(AGENTPLANE_DB_USER):$(AGENTPLANE_DB_PASSWORD)"
                "@$(AGENTPLANE_DB_HOST):$(AGENTPLANE_DB_PORT)/$(AGENTPLANE_DB_NAME)"
            ),
            "AGENTPLANE_OIDC_ISSUER": EnvValue.from_value(self.spec.oidc_issuer),
            "AGENTPLANE_OIDC_PUBLIC_BASE_URL": EnvValue.from_value(f"https://{self.spec.hostname}"),
            "AGENTPLANE_OIDC_CLIENT_ID": EnvValue.from_secret_value(SecretValue(secret=oidc_secret, key="client-id")),
            "AGENTPLANE_OIDC_CLIENT_SECRET": EnvValue.from_secret_value(
                SecretValue(secret=oidc_secret, key="client-secret")
            ),
            "AGENTPLANE_OIDC_SESSION_SECRET": EnvValue.from_secret_value(
                SecretValue(secret=oidc_secret, key="session-secret")
            ),
            "AGENTPLANE_TOKEN_SUBJECTS": EnvValue.from_value(token_subjects),
        }

    def _add_deployment(self, app_service_account: ServiceAccount) -> Deployment:
        namespace = self.spec.namespace
        env = self._container_env()
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                _NAME,
                namespace,
                labels=_LABELS,
                annotations={
                    # A re-minted client secret otherwise leaves the pod on the old
                    # one, and every login 401s.
                    "secret.reloader.stakater.com/reload": "agentplane-oidc",
                    "configmap.reloader.stakater.com/reload": "agentplane-action-federation",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=self.spec.replicas,
            strategy=self.spec.strategy,
            min_ready=Duration.seconds(self.spec.min_ready_seconds)
            if self.spec.min_ready_seconds is not None
            else None,
            # 5s HTTP/SSE drain (--shutdown-timeout), with room for the bridge's lease
            # release and the store's close.
            termination_grace_period=Duration.seconds(60),
            service_account=app_service_account,
            # cdk8s_plus_34 defaults this to False independent of the ServiceAccount's
            # own automount_token -- opt in for the same reason as the ServiceAccount.
            automount_service_account_token=True,
            docker_registry_auth=Secret.from_secret_name(self, "forgejo-images-creds-ref", "forgejo-images-creds"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
            init_containers=[
                # The app's Alembic history. The app itself creates no tables; it
                # verifies the migrated schema at startup and fails if this hasn't run.
                ContainerProps(
                    name="migrate",
                    image=f"{_MIGRATE_IMAGE}:{_PLACEHOLDER_TAG}",
                    image_pull_policy=ImagePullPolicy.ALWAYS,
                    env_variables=env,
                    resources=ContainerResources(
                        cpu=CpuResources(request=Cpu.millis(25)),
                        memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
                    ),
                    security_context=ContainerSecurityContextProps(
                        allow_privilege_escalation=False,
                        capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
                        read_only_root_filesystem=False,
                    ),
                )
            ],
        )
        deployment.add_container(
            name="app",
            image=f"{_APP_IMAGE}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=[
                f"--namespace={namespace}",
                # The same namespace for now: the split the flag exists for is a
                # separate change, which moves the Sandboxes, their template, and the
                # runner ServiceAccount out of here.
                f"--sandbox-namespace={namespace}",
                "--runner-port=7000",
                "--host=0.0.0.0",
                f"--port={_CONTAINER_PORT}",
            ],
            env_variables=env,
            ports=[ContainerPort(name="http", number=_CONTAINER_PORT, protocol=Protocol.TCP)],
            readiness=http_probe("/readyz", port=_CONTAINER_PORT, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=_CONTAINER_PORT, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            # Same rationale as litellm_constructs.py's container securityContext
            # override: the container's actual root needs haven't been audited.
            security_context=ContainerSecurityContextProps(
                allow_privilege_escalation=False,
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
                read_only_root_filesystem=False,
            ),
        )
        config = ConfigMap.from_config_map_name(self, "app-config-ref", "agentplane-app-config")
        volume = Volume.from_config_map(self, "config-volume", config)
        deployment.containers[0].mount("/etc/agentplane", volume, read_only=True)

        # With the database (cnpg_conventions R5); it had no pin at all while the
        # database was Proxmox-single, which is the rule already unmet rather than a
        # new constraint. Unlike llm-ingress/egress, the app carries no control-plane
        # toleration.
        deployment.scheduling.attract(Node.labeled(NodeLabelQuery.is_("topology.kubernetes.io/zone", _ZONE)))

        # cdk8s_plus_34's PodSecurityContextProps has no seccompProfile builder (only
        # ContainerSecurityContextProps does) -- patch the pod-level field directly,
        # same escape hatch used elsewhere in this PR.
        pod_spec_patches = [
            JsonPatch.add("/spec/template/spec/securityContext/seccompProfile", {"type": "RuntimeDefault"})
        ]
        if self.spec.topology_spread:
            pod_spec_patches.append(
                JsonPatch.add(
                    "/spec/template/spec/topologySpreadConstraints",
                    [
                        k8s.TopologySpreadConstraint(
                            max_skew=1,
                            topology_key="kubernetes.io/hostname",
                            when_unsatisfiable="ScheduleAnyway",
                            label_selector=k8s.LabelSelector(match_labels=_LABELS),
                        )
                    ],
                )
            )
        for patch in pod_spec_patches:
            ApiObject.of(deployment).add_json_patch(patch)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(_NAME, self.spec.namespace, labels=_LABELS),
            selector=deployment,
            ports=[ServicePort(name="http", port=_CONTAINER_PORT, target_port=_CONTAINER_PORT, protocol=Protocol.TCP)],
        )

    def _add_http_route(self) -> None:
        namespace = self.spec.namespace
        HttpRoute(
            self,
            "httproute",
            metadata=metadata(namespace, namespace),
            spec=HttpRouteSpec(
                parent_refs=[
                    HttpRouteSpecParentRefs(
                        name="cluster-gateway",
                        namespace="gateway-system",
                        # Not the plaintext listener: the gateway's HTTP-only route
                        # owns port 80 and redirects it.
                        section_name="https-wildcard",
                    )
                ],
                hostnames=[self.spec.hostname],
                rules=[
                    HttpRouteSpecRules(
                        filters=[
                            # Set at the TLS-aware edge; the app's own hop cannot tell
                            # that HTTPS was terminated.
                            HttpRouteSpecRulesFilters(
                                type=HttpRouteSpecRulesFiltersType.RESPONSE_HEADER_MODIFIER,
                                response_header_modifier=HttpRouteSpecRulesFiltersResponseHeaderModifier(
                                    set=[
                                        HttpRouteSpecRulesFiltersResponseHeaderModifierSet(
                                            name="Strict-Transport-Security", value="max-age=31536000"
                                        )
                                    ]
                                ),
                            )
                        ],
                        backend_refs=[HttpRouteSpecRulesBackendRefs(name=_NAME, port=_CONTAINER_PORT)],
                        # A session stream stays attached for as long as the tab is open.
                        timeouts=HttpRouteSpecRulesTimeouts(request="3600s", backend_request="3600s"),
                    )
                ],
            ),
        )

    def _oidc_egress_rules(self) -> list[CiliumNetworkPolicySpecEgress]:
        server_name = urlsplit(self.spec.oidc_issuer).hostname
        assert server_name is not None, f"OIDC issuer has no hostname: {self.spec.oidc_issuer!r}"
        rules = [
            # Public OIDC origin resolves to hostNetwork Gateway node IPs. FQDN/CIDR
            # selectors cannot match those identities with the cluster's current
            # Cilium configuration. TLS SNI restricts the node:443 rule to this one
            # origin; TLS remains end-to-end (no terminatingTLS secret or MITM).
            CiliumNetworkPolicySpecEgress(
                to_entities=[
                    CiliumNetworkPolicySpecEgressToEntities.REMOTE_HYPHEN_NODE,
                    CiliumNetworkPolicySpecEgressToEntities.HOST,
                ],
                to_ports=[
                    CiliumNetworkPolicySpecEgressToPorts(
                        ports=[
                            CiliumNetworkPolicySpecEgressToPortsPorts(
                                port="443", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                            )
                        ],
                        server_names=[server_name],
                    )
                ],
            )
        ]
        if self.spec.reach_incluster_authentik:
            # Gateway Service traffic is checked against the selected backend, with
            # the client's original SNI.
            rules.append(
                CiliumNetworkPolicySpecEgress(
                    to_endpoints=[
                        CiliumNetworkPolicySpecEgressToEndpoints(
                            match_labels={
                                "k8s:io.kubernetes.pod.namespace": "authentik",
                                "app.kubernetes.io/name": "authentik",
                                "app.kubernetes.io/instance": "authentik",
                                "app.kubernetes.io/component": "server",
                            }
                        )
                    ],
                    to_ports=[
                        CiliumNetworkPolicySpecEgressToPorts(
                            ports=[
                                CiliumNetworkPolicySpecEgressToPortsPorts(
                                    port="9000", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                )
                            ],
                            server_names=[server_name],
                        )
                    ],
                )
            )
        return rules

    def _add_network_policy(self) -> None:
        namespace = self.spec.namespace
        dns_egress = CiliumNetworkPolicySpecEgress(
            to_endpoints=[
                CiliumNetworkPolicySpecEgressToEndpoints(
                    match_labels={"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}
                )
            ],
            to_ports=[
                CiliumNetworkPolicySpecEgressToPorts(
                    ports=[
                        CiliumNetworkPolicySpecEgressToPortsPorts(
                            port="53", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.UDP
                        ),
                        CiliumNetworkPolicySpecEgressToPortsPorts(
                            port="53", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                        ),
                    ]
                )
            ],
        )
        # Runner Pods reach DNS and the egress proxy's listener, which the sidecar
        # relays to; port 7000 is open only to Pods in this namespace.
        CiliumNetworkPolicy(
            self,
            "networkpolicy-runner",
            metadata=metadata("agentplane-runner", namespace),
            spec=CiliumNetworkPolicySpec(
                endpoint_selector=CiliumNetworkPolicySpecEndpointSelector(match_labels=_RUNNER_LABELS),
                ingress=[
                    CiliumNetworkPolicySpecIngress(
                        from_endpoints=[
                            CiliumNetworkPolicySpecIngressFromEndpoints(
                                match_labels={"k8s:io.kubernetes.pod.namespace": namespace}
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecIngressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecIngressToPortsPorts(
                                        port="7000", protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP
                                    )
                                ]
                            )
                        ],
                    )
                ],
                egress=[
                    dns_egress,
                    CiliumNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumNetworkPolicySpecEgressToEndpoints(
                                match_labels={
                                    "k8s:io.kubernetes.pod.namespace": namespace,
                                    "app.kubernetes.io/name": "agentplane-egress",
                                }
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecEgressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="8888", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                    )
                                ]
                            )
                        ],
                    ),
                ],
            ),
        )
        # The app takes browser traffic straight from the gateway and reaches DNS, the
        # API server, the OIDC provider, the runner Pods, the egress proxy's admin
        # port, the Action Service, and the trajectory store.
        CiliumNetworkPolicy(
            self,
            "networkpolicy-app",
            metadata=metadata(_NAME, namespace),
            spec=CiliumNetworkPolicySpec(
                endpoint_selector=CiliumNetworkPolicySpecEndpointSelector(match_labels=_LABELS),
                ingress=[
                    # cilium-envoy is hostNetwork and its egress to a backend Pod
                    # carries the reserved:ingress identity.
                    CiliumNetworkPolicySpecIngress(
                        from_entities=[CiliumNetworkPolicySpecIngressFromEntities.INGRESS],
                        to_ports=[
                            CiliumNetworkPolicySpecIngressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecIngressToPortsPorts(
                                        port=str(_CONTAINER_PORT),
                                        protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP,
                                    )
                                ]
                            )
                        ],
                    )
                ],
                egress=[
                    dns_egress,
                    CiliumNetworkPolicySpecEgress(
                        to_entities=[CiliumNetworkPolicySpecEgressToEntities.KUBE_HYPHEN_APISERVER]
                    ),
                    *self._oidc_egress_rules(),
                    CiliumNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumNetworkPolicySpecEgressToEndpoints(
                                match_labels={
                                    "k8s:io.kubernetes.pod.namespace": namespace,
                                    "app.kubernetes.io/name": "agentplane-runner",
                                }
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecEgressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="7000", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                    )
                                ]
                            )
                        ],
                    ),
                    CiliumNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumNetworkPolicySpecEgressToEndpoints(
                                match_labels={
                                    "k8s:io.kubernetes.pod.namespace": namespace,
                                    "app.kubernetes.io/name": "agentplane-egress",
                                }
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecEgressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="8081", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                    )
                                ]
                            )
                        ],
                    ),
                    # Separate BFF/operator transport boundary. The Action Service
                    # still requires its own configured operator authenticator;
                    # network reachability grants no review authority.
                    CiliumNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumNetworkPolicySpecEgressToEndpoints(
                                match_labels={
                                    "k8s:io.kubernetes.pod.namespace": namespace,
                                    "app.kubernetes.io/name": "agentplane-actions",
                                }
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecEgressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="8080", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                    )
                                ]
                            )
                        ],
                    ),
                    CiliumNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumNetworkPolicySpecEgressToEndpoints(
                                match_labels={
                                    "k8s:io.kubernetes.pod.namespace": namespace,
                                    "k8s:cnpg.io/cluster": "postgres",
                                }
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecEgressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="5432", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                    )
                                ]
                            )
                        ],
                    ),
                ],
            ),
        )

    def _add_pdb(self) -> None:
        k8s.KubePodDisruptionBudget(
            self,
            "pdb",
            metadata=k8s.ObjectMeta(name=_NAME, namespace=self.spec.namespace),
            spec=k8s.PodDisruptionBudgetSpec(
                min_available=k8s.IntOrString.from_number(1), selector=k8s.LabelSelector(match_labels=_LABELS)
            ),
        )

    def _runner_container(self) -> SandboxTemplateSpecPodTemplateSpecContainers:
        litellm_url = f"http://agentplane-llm-ingress.{self.spec.namespace}.svc.cluster.local:8080"
        # The environment a harness child starts from: a bare NAME takes the runner's
        # value, NAME=value sets one. Both spellings of proxy vars, since clients
        # disagree on case; NO_PROXY is loopback and nothing else.
        harness_env = [
            "HOME",
            "PATH",
            "HTTP_PROXY=http://127.0.0.1:3128",
            "HTTPS_PROXY=http://127.0.0.1:3128",
            "http_proxy=http://127.0.0.1:3128",
            "https_proxy=http://127.0.0.1:3128",
            "NO_PROXY=127.0.0.1,localhost",
            "no_proxy=127.0.0.1,localhost",
            "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
            "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt",
            "CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt",
            "GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt",
            "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt",
        ]
        args = [
            "--state-dir",
            "/state",
            "--listen",
            "0.0.0.0:7000",
            "--claude-binary",
            "/usr/local/bin/claude",
            "--anthropic-base-url",
            "$(LITELLM_URL)",
            "--codex-binary",
            "/opt/codex/bin/codex",
            "--openai-base-url",
            "$(LITELLM_URL)/v1",
        ]
        for entry in harness_env:
            args.extend(["--harness-env", entry])
        return SandboxTemplateSpecPodTemplateSpecContainers(
            name="runner",
            image=f"{_RUNNER_IMAGE}:{_PLACEHOLDER_TAG}",
            args=args,
            ports=[SandboxTemplateSpecPodTemplateSpecContainersPorts(name="runner", container_port=7000)],
            security_context=SandboxTemplateSpecPodTemplateSpecContainersSecurityContext(
                allow_privilege_escalation=False,
                capabilities=SandboxTemplateSpecPodTemplateSpecContainersSecurityContextCapabilities(drop=["ALL"]),
            ),
            env=[
                SandboxTemplateSpecPodTemplateSpecContainersEnv(name="LITELLM_URL", value=litellm_url),
                # Neither a workload token nor a LiteLLM key: the placeholder the
                # agentplane-workload EgressCredential derives from its name. Central
                # substitutes the sidecar-only projected token; the ingress replaces
                # that with its server-held LiteLLM virtual key after live
                # WorkloadPrincipal resolution.
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name="ANTHROPIC_AUTH_TOKEN", value="agentplane-credential-agentplane-workload"
                ),
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name="OPENAI_API_KEY", value="agentplane-credential-agentplane-workload"
                ),
            ],
            resources=SandboxTemplateSpecPodTemplateSpecContainersResources(
                requests={
                    "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("500m"),
                    "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("1Gi"),
                },
                limits={
                    "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("2"),
                    "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("4Gi"),
                },
            ),
            volume_mounts=[
                SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(name="state", mount_path="/state"),
                # Public roots + cluster root + the proxy's interception root, over
                # the image's own bundle at the path every client falls back to. A
                # subPath mount does not follow ConfigMap updates: a CA rotation
                # reaches a sandbox at its next Pod.
                SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
                    name="egress-ca",
                    mount_path="/etc/ssl/certs/ca-certificates.crt",
                    sub_path="ca-certificates.crt",
                    read_only=True,
                ),
            ],
        )

    def _egress_sidecar_container(self) -> SandboxTemplateSpecPodTemplateSpecContainers:
        namespace = self.spec.namespace
        return SandboxTemplateSpecPodTemplateSpecContainers(
            name="egress-sidecar",
            image=f"{_EGRESS_SIDECAR_IMAGE}:{_PLACEHOLDER_TAG}",
            env=[
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name="AGENTPLANE_EGRESS_SIDECAR_PROXY_HOST",
                    value=f"agentplane-egress.{namespace}.svc.cluster.local",
                ),
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name="AGENTPLANE_EGRESS_SIDECAR_PROXY_PORT", value="8888"
                ),
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name="AGENTPLANE_EGRESS_SIDECAR_LISTEN_PORT", value="3128"
                ),
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name="AGENTPLANE_EGRESS_SIDECAR_TOKEN_FILE", value="/var/run/agentplane-egress/token"
                ),
            ],
            security_context=SandboxTemplateSpecPodTemplateSpecContainersSecurityContext(
                allow_privilege_escalation=False,
                capabilities=SandboxTemplateSpecPodTemplateSpecContainersSecurityContextCapabilities(drop=["ALL"]),
            ),
            resources=SandboxTemplateSpecPodTemplateSpecContainersResources(
                requests={
                    "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("10m"),
                    "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("32Mi"),
                },
                limits={"memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("128Mi")},
            ),
            volume_mounts=[
                SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
                    name="egress-token", mount_path="/var/run/agentplane-egress", read_only=True
                )
            ],
        )

    def _add_sandbox_template(self) -> None:
        namespace = self.spec.namespace
        node_selector = (
            {"topology.kubernetes.io/zone": self.spec.runner_zone} if self.spec.runner_zone is not None else None
        )

        SandboxTemplate(
            self,
            "sandboxtemplate",
            metadata=metadata("agentplane-runner", namespace),
            spec=SandboxTemplateSpec(
                # The CiliumNetworkPolicy next to this construct is the runner's fence.
                network_policy_management=SandboxTemplateSpecNetworkPolicyManagement.UNMANAGED,
                volume_claim_templates_policy=SandboxTemplateSpecVolumeClaimTemplatesPolicy.OVERRIDES,
                pod_template=SandboxTemplateSpecPodTemplate(
                    metadata=SandboxTemplateSpecPodTemplateMetadata(labels=_RUNNER_LABELS),
                    spec=SandboxTemplateSpecPodTemplateSpec(
                        containers=[self._runner_container(), self._egress_sidecar_container()],
                        automount_service_account_token=False,
                        image_pull_secrets=[
                            SandboxTemplateSpecPodTemplateSpecImagePullSecrets(name="forgejo-images-creds")
                        ],
                        # With the rest of the namespace and with LiteLLM: a runner's
                        # model calls and its hop to the app both stay inside the zone.
                        node_selector=node_selector,
                        service_account_name="agentplane-runner",
                        termination_grace_period_seconds=60,
                        security_context=SandboxTemplateSpecPodTemplateSpecSecurityContext(
                            run_as_non_root=True,
                            run_as_user=1000,
                            run_as_group=1000,
                            fs_group=1000,
                            seccomp_profile=SandboxTemplateSpecPodTemplateSpecSecurityContextSeccompProfile(
                                type="RuntimeDefault"
                            ),
                        ),
                        volumes=[
                            SandboxTemplateSpecPodTemplateSpecVolumes(
                                name="egress-ca",
                                config_map=SandboxTemplateSpecPodTemplateSpecVolumesConfigMap(
                                    name=self.spec.runner_ca_configmap_name
                                ),
                            ),
                            # The Pod's identity to the central proxy: a ServiceAccount
                            # token bound to this Pod, with the proxy's audience,
                            # rotated by kubelet.
                            SandboxTemplateSpecPodTemplateSpecVolumes(
                                name="egress-token",
                                projected=SandboxTemplateSpecPodTemplateSpecVolumesProjected(
                                    sources=[
                                        SandboxTemplateSpecPodTemplateSpecVolumesProjectedSources(
                                            service_account_token=SandboxTemplateSpecPodTemplateSpecVolumesProjectedSourcesServiceAccountToken(
                                                audience="agentplane-egress", expiration_seconds=600, path="token"
                                            )
                                        )
                                    ]
                                ),
                            ),
                        ],
                    ),
                ),
                volume_claim_templates=[
                    SandboxTemplateSpecVolumeClaimTemplates(
                        metadata=SandboxTemplateSpecVolumeClaimTemplatesMetadata(name="state"),
                        spec=SandboxTemplateSpecVolumeClaimTemplatesSpec(
                            # The bulk tier, and the only one a sandbox can have: OVH's
                            # `tier=ssd` nodes are control-plane, and a sandbox is not
                            # getting that toleration. Node-local, so a sandbox does
                            # not outlive its node -- which is what a sandbox is for.
                            storage_class_name="local-path-ovh-hdd",
                            access_modes=["ReadWriteOnce"],
                            resources=SandboxTemplateSpecVolumeClaimTemplatesSpecResources(
                                requests={
                                    "storage": SandboxTemplateSpecVolumeClaimTemplatesSpecResourcesRequests.from_string(
                                        "10Gi"
                                    )
                                }
                            ),
                        ),
                    )
                ],
            ),
        )
