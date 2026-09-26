"""sshpiperd for the public-coder Agent's SSH route to its devbox (design and recordings:
cluster/k8s/agents/public-coder-agent/sshpiper/README.md).

The same split as the proxy, applied to SSH instead of HTTP: the Agent authenticates to this Pod
with a key that is worthless anywhere else, and this Pod holds the key that actually opens
`coder@public-coder-devbox`. The Agent never possesses the upstream credential. Unlike HTTP, the
substitution cannot happen in flight -- an SSH publickey signature covers the session id, so a
proxy has to terminate and re-originate. That is what sshpiper's "mapping key" model does, and it
is why the Agent pins *this* Pod's host key rather than the devbox's.

The routing `Pipe` is `ssh_mcp.sshpiper`'s, written into the same directory.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct

from cluster.cdk8s import cilium
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy

NAME = "public-coder-agent-sshpiper"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/public-coder-agent/sshpiper"
_NAMESPACE = "public-coder-agent"
LABELS = {"app.kubernetes.io/name": NAME}
PORT = 2222
_HOST_KEY_SECRET_NAME = "public-coder-agent-sshpiper-host-key"
_RECORDINGS_CLAIM_NAME = "public-coder-agent-sshpiper-recordings"
# Match MODULE.bazel's sshpiper_pipe_crd tag.
_IMAGE = "farmer1992/sshpiperd:v1.6.1@sha256:9ddc25422cc2d7236d7704230b7a706d4c39518edfd211275186b725bf9d3da1"
_RESOURCES = [
    "host-key.sops.yaml",
    "devbox-key.sops.yaml",
    f"{NAME}.k8s.yaml",
    # ssh_mcp.generation's Pipe.
    "pipe-devbox.k8s.yaml",
]


def _rbac(scope: Construct) -> None:
    """The kubernetes plugin reads its routing table from the API server, so the piper Pod needs
    an API identity -- the one thing in this namespace that does (the proxy sets
    automountServiceAccountToken: false). Namespaced Role, and the plugin is started without
    --all-namespaces, so the piper can only ever route what this namespace declares.

    `secrets` is read-only and unavoidable: Pipe.spec.to.private_key_secret names the upstream
    mapping key by reference, and the plugin resolves it at connection time. That makes this
    ServiceAccount able to read *every* Secret in public-coder-agent, including the proxy's
    GitHub PAT and Console bearer -- a real widening, and the reason the piper runs as its own
    workload with its own network policy rather than as a container in the proxy Pod.

    Deliberately absent: pods/exec. Upstream's sample grants it for the kubectl-exec bridge mode,
    which pipes into a Pod without sshd. Our upstream is a KubeVirt VM running real sshd, so that
    mode is unused and granting exec would hand this ServiceAccount a shell in every Pod here.
    """
    k8s.KubeServiceAccount(scope, "service-account", metadata=k8s.ObjectMeta(name=NAME, namespace=_NAMESPACE))
    k8s.KubeRole(
        scope,
        "role",
        metadata=k8s.ObjectMeta(name=NAME, namespace=_NAMESPACE),
        rules=[
            k8s.PolicyRule(api_groups=["sshpiper.com"], resources=["pipes"], verbs=["get", "list", "watch"]),
            k8s.PolicyRule(api_groups=[""], resources=["secrets"], verbs=["get"]),
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "role-binding",
        metadata=k8s.ObjectMeta(name=NAME, namespace=_NAMESPACE),
        subjects=[k8s.Subject(kind="ServiceAccount", name=NAME, namespace=_NAMESPACE)],
        role_ref=k8s.RoleRef(kind="Role", name=NAME, api_group="rbac.authorization.k8s.io"),
    )


def _recordings_claim(scope: Construct) -> None:
    k8s.KubePersistentVolumeClaim(
        scope,
        "recordings",
        metadata=k8s.ObjectMeta(
            name=_RECORDINGS_CLAIM_NAME,
            namespace=_NAMESPACE,
            annotations={
                "description": (
                    "Session recordings. This is the audit trail for the SSH route: unlike an MCP tool call, "
                    "which leaves a structured per-call `mcp_tool_calls` row, a shell session has no equivalent "
                    "structured record, so the recordings are what remains. Local-path is RWO and binds to one "
                    "node, which is why the Deployment stays at one replica."
                )
            },
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="local-path-ovh",
            # Asciicast is text; a long build's output is megabytes, not gigabytes. Nothing prunes
            # this yet -- see README.md § Recordings.
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("5Gi")}),
        ),
    )


def _env(name: str, value: str) -> k8s.EnvVar:
    return k8s.EnvVar(name=name, value=value)


def _container() -> k8s.Container:
    ssh_port = k8s.IntOrString.from_string("ssh")
    return k8s.Container(
        name="sshpiperd",
        image=_IMAGE,
        env=[
            _env("PLUGIN", "kubernetes"),
            _env("SSHPIPERD_SERVER_KEY", "/host-key/ssh_host_ed25519_key"),
            # Already upstream's default, pinned because a flip to `notexist` would silently mint a
            # fresh host key whenever the Secret failed to mount -- which the Agent cannot
            # distinguish from a MITM. Failing to start is the better outcome.
            _env("SSHPIPERD_SERVER_KEY_GENERATE_MODE", "disable"),
            _env("SSHPIPERD_LOG_LEVEL", "info"),
            # The route exists for shells and file copies, not for tunnels. `ssh -L`/`-D` would let
            # the Agent reach anything the devbox can reach, and `ssh -R` would let it open
            # listeners there; neither is what this door is for. Both are one env var away if a
            # real need turns up.
            _env("SSHPIPERD_DISABLE_LOCAL_FORWARDING", "true"),
            _env("SSHPIPERD_DISABLE_REMOTE_FORWARDING", "true"),
            # The upstream advertises its own host keys over hostkeys-00@openssh.com. Forwarding
            # that to the Agent would offer it the devbox's keys as if they were this piper's,
            # which is at best a client-side warning and at worst a poisoned known_hosts.
            _env("SSHPIPERD_DROP_HOSTKEYS_MESSAGE", "true"),
            # asciicast captures PTY sessions (`ssh devbox`, and the streaming build output this
            # route exists for). A non-PTY `ssh devbox <cmd>` is not screen output and is not
            # captured -- see README.md § Recordings.
            _env("SSHPIPERD_SCREEN_RECORDING_FORMAT", "asciicast"),
            _env("SSHPIPERD_SCREEN_RECORDING_DIR", "/recordings"),
        ],
        ports=[k8s.ContainerPort(name="ssh", container_port=PORT)],
        security_context=k8s.SecurityContext(
            allow_privilege_escalation=False,
            capabilities=k8s.Capabilities(drop=["ALL"]),
            read_only_root_filesystem=True,
        ),
        liveness_probe=k8s.Probe(
            tcp_socket=k8s.TcpSocketAction(port=ssh_port), initial_delay_seconds=10, period_seconds=30
        ),
        readiness_probe=k8s.Probe(tcp_socket=k8s.TcpSocketAction(port=ssh_port), period_seconds=10),
        volume_mounts=[
            k8s.VolumeMount(name="host-key", mount_path="/host-key", read_only=True),
            k8s.VolumeMount(name="recordings", mount_path="/recordings"),
            k8s.VolumeMount(name="tmp", mount_path="/tmp"),
        ],
        resources=k8s.ResourceRequirements(
            requests={"cpu": k8s.Quantity.from_string("25m"), "memory": k8s.Quantity.from_string("64Mi")},
            limits={"cpu": k8s.Quantity.from_string("500m"), "memory": k8s.Quantity.from_string("256Mi")},
        ),
    )


def _deployment(scope: Construct) -> None:
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME, namespace=_NAMESPACE, labels=LABELS, annotations={"reloader.stakater.com/auto": "true"}
        ),
        spec=k8s.DeploymentSpec(
            # One replica, and not only because the recordings PVC is RWO: two pipers would each
            # need the host key, and a client reconnecting to the other one is indistinguishable
            # from a MITM.
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=LABELS),
                spec=k8s.PodSpec(
                    service_account_name=NAME,
                    # Unlike the proxy, this Pod does need its API token: the kubernetes plugin
                    # reads Pipes and resolves the upstream key Secret through the API server.
                    automount_service_account_token=True,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        # The image's own `sshpiperd` account. Stated rather than inherited so the
                        # key Secret's mode below is meaningful, and so this Pod does not depend on
                        # the image's USER directive.
                        run_as_user=1000,
                        run_as_group=1000,
                        # Makes the host key readable at 0440 and gives the recordings volume a
                        # writable group.
                        fs_group=1000,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[_container()],
                    volumes=[
                        k8s.Volume(
                            name="host-key",
                            # 0440, not 0400: with fsGroup the projected file is root:1000, so
                            # owner-only would be unreadable by the container's own user.
                            secret=k8s.SecretVolumeSource(secret_name=_HOST_KEY_SECRET_NAME, default_mode=0o440),
                        ),
                        k8s.Volume(
                            name="recordings",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(
                                claim_name=_RECORDINGS_CLAIM_NAME
                            ),
                        ),
                        # readOnlyRootFilesystem leaves nothing writable otherwise.
                        k8s.Volume(name="tmp", empty_dir=k8s.EmptyDirVolumeSource()),
                    ],
                ),
            ),
        ),
    )


def _service(scope: Construct) -> None:
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=_NAMESPACE,
            labels=LABELS,
            annotations={
                "description": (
                    "ClusterIP for the Agent's SSH route to the devbox. Not published outside the cluster; "
                    "reachability is decided by cnp-ingress.yaml, not by this Service existing."
                )
            },
        ),
        spec=k8s.ServiceSpec(
            type="ClusterIP",
            selector=LABELS,
            ports=[
                k8s.ServicePort(name="ssh", protocol="TCP", port=PORT, target_port=k8s.IntOrString.from_number(PORT))
            ],
        ),
    )


def _network_policies(scope: Construct, app_namespace: str, app_labels: dict[str, str]) -> None:
    # The piper holds the key that opens `coder@public-coder-devbox`, so possession of a route to
    # this listener is most of the authority. Only the OpenClaw Agent Pod gets one -- namespace
    # co-tenancy is not authority to use it, the same rule the proxy's ingress policy states. The
    # devbox itself is deliberately absent: it is the upstream, it has no reason to dial the piper.
    #
    # This is a fence, not the credential boundary. Reaching the listener is worth nothing without
    # a key the Pipe's authorized_keys_data accepts.
    NetworkPolicy(
        scope,
        "ingress",
        metadata=metadata("allow-public-coder-agent-sshpiper-ingress", _NAMESPACE),
        selector=LABELS,
        ingress=[
            IngressRule.from_endpoints(
                {
                    "k8s:io.kubernetes.pod.namespace": app_namespace,
                    **{f"k8s:{key}": value for key, value in app_labels.items()},
                },
                ports=[PORT],
            )
        ],
    )
    # Confined, unlike the proxy's egress. The proxy's waiver exists because that Agent reads
    # arbitrary public repositories; the piper has exactly one upstream and no reason to reach
    # anything else, so this stays an allowlist and every widening is a diff here.
    NetworkPolicy(
        scope,
        "egress",
        metadata=metadata("allow-public-coder-agent-sshpiper-egress", _NAMESPACE),
        selector=LABELS,
        egress=[
            cilium.dns_egress(protocols=["ANY"], resolves=["*"]),
            # The kubernetes plugin watches Pipes and resolves the upstream key Secret through the
            # API server; without this the piper starts and then refuses every connection.
            EgressRule.to_entities("kube-apiserver"),
            # The one upstream. KubeVirt gives the VM an ordinary Pod identity, so the guest's sshd
            # is selectable by the VM's domain label.
            EgressRule.to_endpoints(
                {"k8s:io.kubernetes.pod.namespace": _NAMESPACE, "k8s:kubevirt.io/domain": "public-coder-devbox"}, 22
            ),
        ],
    )


def chart(app: App, *, app_namespace: str, app_labels: dict[str, str]) -> Chart:
    """`app_namespace` and `app_labels` are the OpenClaw Agent pod's: public_coder_agent_config
    exports them, and imports this module for the piper's address."""
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _rbac(chart)
    _recordings_claim(chart)
    _deployment(chart)
    _service(chart)
    _network_policies(chart, app_namespace, app_labels)
    return chart


def write_manifests(root: Path, *, app_namespace: str, app_labels: dict[str, str]) -> None:
    write_charts(root, OUTPUT_DIR, lambda app: chart(app, app_namespace=app_namespace, app_labels=app_labels))
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=_RESOURCES))
