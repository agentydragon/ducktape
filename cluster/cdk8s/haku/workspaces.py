"""haku-workspaces: the Haku exec-target sandbox template in haku-sandbox, the credentials its
pods and runtimes read, the claim/exec Role Console hands sandboxes out with, and the janitor.

Also the warm pool. Hand-written beside the output: `image-pins/kustomization.yaml`, which
overrides the workspace image's `unset` tag.
"""

from __future__ import annotations

from pathlib import Path

from agent_sandbox_sandboxtemplate_crds.io.x_k8s.agents.extensions import (
    SandboxTemplate,
    SandboxTemplateSpec,
    SandboxTemplateSpecEnvVarsInjectionPolicy,
    SandboxTemplateSpecNetworkPolicyManagement,
    SandboxTemplateSpecPodTemplate,
    SandboxTemplateSpecPodTemplateMetadata,
    SandboxTemplateSpecPodTemplateSpec,
    SandboxTemplateSpecPodTemplateSpecContainers,
    SandboxTemplateSpecPodTemplateSpecContainersEnv,
    SandboxTemplateSpecPodTemplateSpecContainersEnvValueFrom,
    SandboxTemplateSpecPodTemplateSpecContainersEnvValueFromSecretKeyRef,
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
    SandboxTemplateSpecPodTemplateSpecVolumesEmptyDir,
    SandboxTemplateSpecPodTemplateSpecVolumesEmptyDirSizeLimit,
)
from agent_sandbox_sandboxwarmpool_crds.io.x_k8s.agents.extensions import (
    SandboxWarmPool,
    SandboxWarmPoolSpec,
    SandboxWarmPoolSpecSandboxTemplateRef,
    SandboxWarmPoolSpecUpdateStrategy,
    SandboxWarmPoolSpecUpdateStrategyType,
)
from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromExtract,
    ExternalSecretSpecDataRemoteRef,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMergePolicy,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from kyverno_cleanuppolicy_crds.io.kyverno import (
    CleanupPolicy,
    CleanupPolicySpec,
    CleanupPolicySpecConditions,
    CleanupPolicySpecConditionsAll,
    CleanupPolicySpecConditionsAllOperator,
    CleanupPolicySpecMatch,
    CleanupPolicySpecMatchAny,
    CleanupPolicySpecMatchAnyResources,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import forgejo_images
from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.haku.namespace import NAMESPACE
from cluster.cdk8s.metadata import metadata

NAME = "haku-workspaces"
OUTPUT_DIR = "cluster/k8s/haku/workspaces/app"
TEMPLATE_NAME = "haku"

_CONSOLE_ROLE = "haku-console-sandbox"


def _external_secrets(chart: Chart) -> None:
    # Mirror the ducktape-ci Forgejo registry pull credential into haku-sandbox so the Haku sandbox
    # pods can pull the sandbox image from the private registry. Reflects the source secret's
    # ready-made .dockerconfigjson verbatim and only stamps the type on top (no reassembly, no
    # hardcoded host); sources the flux-system copy (itself an ExternalSecret against the scoped
    # kubernetes-forgejo-images-secret-store -- cluster/k8s/forgejo-images/).
    #
    # Reads through the wide flux-system store rather than the scoped one, unlike the sibling
    # proxies: haku-sandbox is on the flux-system store's allowlist regardless, for
    # alloy-otlp-bearer and haku-mail-token, so switching stores would narrow nothing.
    ExternalSecret(
        chart,
        "forgejo-images-creds",
        metadata=metadata(forgejo_images.SECRET_NAME, NAMESPACE),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                name="kubernetes-flux-system-secret-store",
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
            ),
            target=ExternalSecretSpecTarget(
                name=forgejo_images.SECRET_NAME,
                template=ExternalSecretSpecTargetTemplate(
                    type="kubernetes.io/dockerconfigjson",
                    merge_policy=ExternalSecretSpecTargetTemplateMergePolicy.MERGE,
                ),
            ),
            data_from=[
                ExternalSecretSpecDataFrom(extract=ExternalSecretSpecDataFromExtract(key=forgejo_images.SECRET_NAME))
            ],
        ),
    )
    # Mirror the read-only ActivityWatch bearer into haku-sandbox so Haku can query the read route
    # (activitywatch-read.allegedly.works: GET plus POST /api/0/query/, read-only by route
    # construction -- cluster/docs/activitywatch/README.md) from any runtime whose kubeconfig can
    # read this namespace, with no per-call approval. The egress-fence placeholder substitution on
    # the sandbox templates stays the path for pods behind the fence; this copy serves the runtimes
    # outside it (the Claude Code web home, hostexec-free reads) and haku-state's `haku aw` CLI.
    ExternalSecret(
        chart,
        "activitywatch-read-token",
        metadata=metadata("activitywatch-read-token", NAMESPACE),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                name="kubernetes-activitywatch-secret-store",
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
            ),
            target=ExternalSecretSpecTarget(name="activitywatch-read-token"),
            data=[
                ExternalSecretSpecData(
                    secret_key="token",
                    remote_ref=ExternalSecretSpecDataRemoteRef(key="activitywatch-read-token", property="token"),
                )
            ],
        ),
    )
    ExternalSecret(
        chart,
        "coinbase-api-credentials",
        metadata=metadata("coinbase-api-credentials", NAMESPACE),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                name="kubernetes-external-creds-secret-store",
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
            ),
            target=ExternalSecretSpecTarget(
                name="haku-sandbox-coinbase-api-credentials",
                creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
            ),
            data=[
                ExternalSecretSpecData(
                    secret_key=key,
                    remote_ref=ExternalSecretSpecDataRemoteRef(key="coinbase-api-credentials", property=key),
                )
                for key in ("api_key", "api_secret")
            ],
        ),
    )


def _sandbox_template(chart: Chart) -> SandboxTemplate:
    """The Haku sandbox: an in-cluster EXEC TARGET haku-console hands out through its in-process
    `sandbox` server. The agent (Claude Code web / claude.ai / a managed agent) execs
    `bazel run //cli:...` / `bazel test //...` in here THROUGH Console, against a git-synced
    haku-state checkout. It does NOT run the agent loop. haku-state builds locally (no
    BuildBuddy/RBE -- source never leaves the cluster), so the image is the local toolchain
    (cluster/k8s/haku/workspaces/image/).

    Trust model (see haku/docs/security.md + haku-state improvements/
    sandbox-provisioning-mcp.md): the pool lives IN haku-sandbox, where Haku already has full
    CRUD -- so Haku CAN edit this template and create pods/claims directly. That's safe because
    the network fence does NOT rest on pod-spec or namespace write-isolation: it's the
    cluster-scoped haku-sandbox-force-proxy CCNP (selects the namespace, enforced at the Cilium
    datapath) + the cluster-default baseline PodSecurity (forbids hostNetwork/hostPort/host*),
    both OUTSIDE Haku's RBAC (its haku-sandbox-admin Role has no cilium/networkpolicy/PSS verbs).
    Every pod in haku-sandbox is force-proxied and baseline-confined regardless of who wrote it,
    so Haku can't craft an un-fenced pod; and a Haku direct-exec ~ the arbitrary bash
    exec_sandbox already grants. Console's approval gate still governs the one party it must --
    the kubeconfig-less external harness (a plain claude.ai chat), which can only reach the box
    THROUGH Console.
    """
    return SandboxTemplate(
        chart,
        "sandbox-template",
        metadata=metadata(TEMPLATE_NAME, NAMESPACE),
        spec=SandboxTemplateSpec(
            # Unmanaged so the controller doesn't stamp its own RFC1918-blocking policy that would
            # fight the haku-egress-proxy fence applied at the namespace level (the existing
            # haku-sandbox-force-proxy CCNP).
            network_policy_management=SandboxTemplateSpecNetworkPolicyManagement.UNMANAGED,
            # Claims may inject env, same as the runner templates: the claim-env rail is how a
            # per-provision caller-bound token reaches the box (#5228 slice 1); without this the
            # controller rejects claim env (`EnvVarsInjectionRejected`, fatal in the sandbox
            # client). Injection authority stays Console's -- only Console creates claims against
            # this pool.
            env_vars_injection_policy=SandboxTemplateSpecEnvVarsInjectionPolicy.ALLOWED,
            pod_template=SandboxTemplateSpecPodTemplate(
                metadata=SandboxTemplateSpecPodTemplateMetadata(labels={"app.kubernetes.io/name": "haku-sandbox"}),
                spec=SandboxTemplateSpecPodTemplateSpec(
                    # Controller defaults DNS to public resolvers only; restore cluster DNS so
                    # forgejo-http.forgejo (module fetch + haku-state clone) resolves.
                    dns_policy="ClusterFirst",
                    image_pull_secrets=[
                        SandboxTemplateSpecPodTemplateSpecImagePullSecrets(name=forgejo_images.SECRET_NAME)
                    ],
                    # No region nodeSelector: any always-on node (hil/home/proxmox) is fine now that
                    # /workspace is an emptyDir (below) rather than a region-pinned PVC. Roaming
                    # nodes (iguana, rugged) stay excluded on their own via their existing
                    # `node-role.kubernetes.io/roaming=true:NoSchedule` taint -- no explicit
                    # exclusion needed here.
                    # TODO(operator, 2026-07-25): roaming nodes are often offline (see
                    # cluster/README.md Node Types), so scheduling a sandbox there needs more
                    # thought (claim durability across a node going away mid-session,
                    # re-provisioning UX) before adding the toleration that would let a claim land
                    # on one. Not done as part of this change.
                    # No Kubernetes credential is mounted. `kubectl` reaches the API only through
                    # haku-kube-api-proxy, which asks Console about every request before forwarding
                    # it under the proxy's own in-cluster credential. Standing authority is
                    # unchanged -- Console SARs the haku access-profile group, bound to the same
                    # haku-sandbox-admin Role the mounted token carried (haku/rbac.py) -- so this
                    # removes an exfiltratable credential rather than access. The per-claim setup
                    # script writes the kubeconfig from the two env vars below.
                    automount_service_account_token=False,
                    security_context=SandboxTemplateSpecPodTemplateSpecSecurityContext(
                        run_as_non_root=True,
                        run_as_user=1000,
                        run_as_group=1000,
                        fs_group=1000,
                        seccomp_profile=SandboxTemplateSpecPodTemplateSpecSecurityContextSeccompProfile(
                            type="RuntimeDefault"
                        ),
                    ),
                    containers=[
                        SandboxTemplateSpecPodTemplateSpecContainers(
                            name="workspace",
                            # image-pins/ sets the tag.
                            image="git.allegedly.works/ducktape-ci/haku-sandbox-image:unset",
                            command=["sleep", "infinity"],
                            working_dir="/workspace",
                            security_context=SandboxTemplateSpecPodTemplateSpecContainersSecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=SandboxTemplateSpecPodTemplateSpecContainersSecurityContextCapabilities(
                                    drop=["ALL"]
                                ),
                            ),
                            env=[
                                # The haku Forgejo credential is redeemed by the colocated egress
                                # fence: this inert password is replaced only in Authorization
                                # headers for the approved Forgejo origins. The per-claim
                                # haku-sandbox-setup.sh writes the pair into ~/.netrc for both
                                # in-cluster git fetches (the ducktape_haku module git_override on
                                # every bazel invocation + the haku-state clone), while the real
                                # credential stays in Console.
                                SandboxTemplateSpecPodTemplateSpecContainersEnv(name="HAKU_GIT_USERNAME", value="haku"),
                                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                                    name="HAKU_GIT_PASSWORD", value="haku-forgejo-token-placeholder"
                                ),
                                # The haku-console agent bearer, so the CLI's console-MCP client
                                # (`cli/console.py`, which prefers this env var over a kubectl
                                # secret read) authenticates with no kubectl and no per-run token
                                # fetch -- `bazel run //cli:haku -- read --all` works out of the
                                # box. Same credential the agent already drives the console with;
                                # keeping it on the pod env grants no authority the sandbox lacked.
                                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                                    name="HAKU_CONSOLE_TOKEN",
                                    value_from=SandboxTemplateSpecPodTemplateSpecContainersEnvValueFrom(
                                        secret_key_ref=SandboxTemplateSpecPodTemplateSpecContainersEnvValueFromSecretKeyRef(
                                            name="haku-console-agent-api", key="token"
                                        )
                                    ),
                                ),
                                # Where `kubectl` sends everything, now that nothing is mounted.
                                # Console authorizes each request against the bearer above;
                                # haku-sandbox-setup.sh turns the pair into ~/.kube/config with the
                                # bearer in a mode-0600 tokenFile. https, never http: client-go
                                # attaches kubeconfig credentials only to a TLS server.
                                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                                    name="HAKU_KUBERNETES_PROXY_URL",
                                    value="https://haku-kube-api-proxy.haku-console.svc.cluster.local:8443",
                                ),
                            ],
                            resources=SandboxTemplateSpecPodTemplateSpecContainersResources(
                                requests={
                                    "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string(
                                        "1"
                                    ),
                                    "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string(
                                        "2Gi"
                                    ),
                                },
                                limits={
                                    "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("3"),
                                    "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string(
                                        "6Gi"
                                    ),
                                },
                            ),
                            volume_mounts=[
                                SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
                                    name="workspace", mount_path="/workspace"
                                )
                            ],
                        )
                    ],
                    # emptyDir, not a PVC: /workspace only ever holds a shallow (--depth 1)
                    # haku-state git checkout, which haku-sandbox-setup.sh unconditionally
                    # fetch+reset --hard's on every claim, so nothing here needs to survive a pod
                    # restart. A network filesystem also costs binding latency and makes Bazel's
                    # project-file scan intermittently ENOENT against SeaweedFS.
                    volumes=[
                        SandboxTemplateSpecPodTemplateSpecVolumes(
                            name="workspace",
                            empty_dir=SandboxTemplateSpecPodTemplateSpecVolumesEmptyDir(
                                size_limit=SandboxTemplateSpecPodTemplateSpecVolumesEmptyDirSizeLimit.from_string(
                                    "30Gi"
                                )
                            ),
                        )
                    ],
                ),
            ),
        ),
    )


def _console_rbac(chart: Chart) -> None:
    # The exact API access Console needs to hand out sandboxes from this pool, scoped to the pool
    # namespace (derived from haku/sandbox/kubernetes_client.py): create/adopt + GC SandboxClaims,
    # read the assigned Sandbox + Pod, and open exec sessions. No secrets, no other verbs.
    k8s.KubeRole(
        chart,
        "console-role",
        metadata=k8s.ObjectMeta(name=_CONSOLE_ROLE, namespace=NAMESPACE),
        rules=[
            k8s.PolicyRule(
                api_groups=["extensions.agents.x-k8s.io"],
                resources=["sandboxclaims"],
                verbs=["create", "get", "list", "watch", "patch", "delete"],
            ),
            k8s.PolicyRule(api_groups=["agents.x-k8s.io"], resources=["sandboxes"], verbs=["get", "list", "watch"]),
            k8s.PolicyRule(api_groups=[""], resources=["pods"], verbs=["get", "list", "watch"]),
            # `get` (not just `create`) is required: Console execs via kubernetes_asyncio, whose
            # helper (connect_get_namespaced_pod_exec) opens the stream with an HTTP GET, so the
            # apiserver authorizes it as `get pods/exec`. kubectl/client-go POST and need `create`;
            # keep both so either handshake style works.
            k8s.PolicyRule(api_groups=[""], resources=["pods/exec"], verbs=["get", "create"]),
        ],
    )
    # Cross-namespace: the subject SA lives in Console's own namespace, the Role + RoleBinding
    # live in the pool namespace it operates on.
    k8s.KubeRoleBinding(
        chart,
        "console-binding",
        metadata=k8s.ObjectMeta(name=_CONSOLE_ROLE, namespace=NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_CONSOLE_ROLE),
        subjects=[k8s.Subject(kind="ServiceAccount", name="haku-console", namespace="haku-console")],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _external_secrets(chart)
    # Consumer-owned identity for reading approved canonical credentials.
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=NAMESPACE)
    )
    sandbox_template = _sandbox_template(chart)
    # One pre-warmed Haku sandbox so a SandboxClaim (from the sandbox-provisioning MCP) is ready
    # in seconds instead of a cold image pull + PVC bind. Costs one idle pod (1 cpu / 2Gi
    # requests) inside the namespace quota; bump replicas only if claims routinely outpace warmup.
    SandboxWarmPool(
        chart,
        "warm-pool",
        metadata=metadata("haku", NAMESPACE),
        spec=SandboxWarmPoolSpec(
            replicas=1,
            update_strategy=SandboxWarmPoolSpecUpdateStrategy(type=SandboxWarmPoolSpecUpdateStrategyType.RECREATE),
            sandbox_template_ref=SandboxWarmPoolSpecSandboxTemplateRef(name=sandbox_template.name),
        ),
    )
    # Same 7-day backstop as the agent-workspaces janitor: a Sandbox/SandboxClaim whose owner
    # forgot shutdownTime would otherwise pin quota forever. Reaping is at the CR level (the
    # controller recreates a Sandbox's pod, so a pod-level janitor just churns). Warm-pool
    # sandboxes reaped at 7d are recreated by the pool -- a harmless periodic refresh. Delete RBAC
    # is the shared kyverno/policies/clusterrole-cleanup-controller-sandboxes.yaml (cluster-wide).
    CleanupPolicy(
        chart,
        "janitor",
        metadata=metadata("haku-workspace-janitor", NAMESPACE),
        spec=CleanupPolicySpec(
            schedule="45 * * * *",
            match=CleanupPolicySpecMatch(
                any=[
                    CleanupPolicySpecMatchAny(
                        resources=CleanupPolicySpecMatchAnyResources(
                            kinds=["agents.x-k8s.io/v1beta1/Sandbox", "extensions.agents.x-k8s.io/v1beta1/SandboxClaim"]
                        )
                    )
                ]
            ),
            conditions=CleanupPolicySpecConditions(
                all=[
                    CleanupPolicySpecConditionsAll(
                        key="{{ time_since('', '{{ target.metadata.creationTimestamp }}', '') }}",
                        operator=CleanupPolicySpecConditionsAllOperator.GREATER_THAN,
                        value="168h",
                    )
                ]
            ),
        ),
    )
    _console_rbac(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"], components=["./image-pins"]),
    )


def haku_workspaces(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    agent_sandbox_controller: Kustomization,
    haku_rbac: Kustomization,
    haku_egress_proxy: Kustomization,
    kyverno_policies: Kustomization,
    external_secrets_config: Kustomization,
) -> Kustomization:
    name = "haku-workspaces"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            source_ref=artifact_source_ref(artifact),
            depends_on=flux_kustomization_depends_on_many(
                # shared CRDs + controller
                agent_sandbox_controller,
                # haku-sandbox ns + haku-sandbox-admin Role the SA rolebinding needs
                haku_rbac,
                # the fence haku-sandbox is opted into
                haku_egress_proxy,
                # CleanupPolicy CRD and cleanup-controller permissions
                kyverno_policies,
                # ESO CRDs and shared ClusterSecretStore
                external_secrets_config,
            ),
        ),
        description="General Haku workspaces in haku-sandbox.",
    )
