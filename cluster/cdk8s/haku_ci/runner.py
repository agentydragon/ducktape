"""haku-ci: the operator-owned namespace running Haku's image builds, the KEDA ScaledJob of
ephemeral Forgejo Actions runners it scales on the queue, and the egress fence forcing their
external traffic through haku-egress-proxy. See cluster/k8s/haku-ci/README.md.

Hand-written beside the output: the runner's `config.yaml` and the directory's
`kustomization.yaml`, whose `configMapGenerator` entry needs options not expressible here.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cilium_clusterwide_crds.io.cilium import (
    CiliumClusterwideNetworkPolicy,
    CiliumClusterwideNetworkPolicySpec,
    CiliumClusterwideNetworkPolicySpecEgress,
    CiliumClusterwideNetworkPolicySpecEgressToEndpoints,
    CiliumClusterwideNetworkPolicySpecEgressToEntities,
    CiliumClusterwideNetworkPolicySpecEgressToPorts,
    CiliumClusterwideNetworkPolicySpecEgressToPortsPorts,
    CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol,
    CiliumClusterwideNetworkPolicySpecEndpointSelector,
    CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressions,
    CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressionsOperator,
)
from keda_scaledjob_crds.sh import keda
from keda_triggerauthentication_crds.sh.keda import (
    TriggerAuthentication,
    TriggerAuthenticationSpec,
    TriggerAuthenticationSpecSecretTargetRef,
)

from cluster.cdk8s import cilium
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "haku-ci"
NAMESPACE = "haku-ci"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/haku-ci"

_RUNNER = "haku-runner"
_LABELS = {"app.kubernetes.io/name": _RUNNER}
_AUTH = "haku-ci-forgejo"
# The hand-written SOPS Secret (haku/forgejo-tea) Reflector copies into NAMESPACE for KEDA.
FORGEJO_TOKEN_SECRET = "haku-forgejo-tea"
FORGEJO_TOKEN_KEY = "token"
_FORGEJO_URL = "http://forgejo-http.forgejo:3000"
_PROXY_URL = "http://haku-egress-proxy.haku-egress-proxy.svc.cluster.local:8080"
_NO_PROXY = (
    "127.0.0.1,localhost,host.docker.internal,*.allegedly.works,.allegedly.works,forgejo-http.forgejo,"
    "forgejo-http.forgejo.svc.cluster.local,.forgejo,.svc.cluster.local,10.0.0.0/8"
)
_CA_FILE = "/egress-proxy-ca/ca-certificates.crt"
_DOCKER_PORT = 2375
# Pinned by digest, not `:act-latest`. dind pulls Docker Hub through oci-cache (Zot, on-demand
# sync), which answers a TAG request only after re-checking upstream whether the tag moved. That
# check has taken 1m46s even with the image already cached, dockerd abandons the mirror at 60s,
# and its direct Docker Hub fallback is blocked by the egress fence: every job queued behind it
# failed in its first minute without running a step. A digest is immutable, so Zot serves it from
# its own store without asking upstream. Bump by hand from the tag's current index digest:
#   docker buildx imagetools inspect catthehacker/ubuntu:act-latest --format '{{.Manifest.Digest}}'
# Zot's first request for a new digest syncs it cold (1m36s observed), which fails jobs the same
# way, so HEAD `http://oci-cache.oci-cache.svc/v2/catthehacker/ubuntu/manifests/<digest>` once
# from inside the cluster before the bump lands.
# Sync: cluster/k8s/haku-ci/config.yaml `runner.labels` must carry the same label.
_JOB_LABEL = (
    "haku-ci:docker://catthehacker/ubuntu@sha256:c58e2b364da03b0c804c7d660f2ecbedf2f221a382b9baa0b344b0144780ff43"
)


def _add_egress_fence(chart: Chart) -> None:
    """Force all external egress from the haku-ci runner (agent-controlled build compute) through
    haku-egress-proxy -- the same chokepoint haku-sandbox uses. Allows: DNS, cluster-internal
    traffic (the in-cluster Forgejo git + OCI registry it clones/pushes/long-polls), and
    haku-egress-proxy port 8080. Blocks all direct external internet -- the base-image registries,
    npm/pypi, Bazel/toolchain, and Forgejo-action hosts now flow through the proxy's allowlist
    (cluster/cdk8s/egress_fences.py).

    Deviation from haku-sandbox's force-egress: no kube-apiserver rule -- haku-ci runs with
    automountServiceAccountToken:false and has no RBAC, so it never calls the k8s API.

    This is the SOLE egress policy for haku-ci: any Cilium egress rule flips the selected pods to
    default-deny egress, so this replaces the old per-namespace CiliumNetworkPolicy (which listed
    direct external FQDNs).
    """

    def ports(
        *numbers: int,
        protocol: CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol = (
            CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol.TCP
        ),
    ) -> list[CiliumClusterwideNetworkPolicySpecEgressToPortsPorts]:
        return [CiliumClusterwideNetworkPolicySpecEgressToPortsPorts(port=str(n), protocol=protocol) for n in numbers]

    CiliumClusterwideNetworkPolicy(
        chart,
        "force-proxy-egress",
        metadata=ApiObjectMetadata(name="haku-ci-force-proxy-egress"),
        spec=CiliumClusterwideNetworkPolicySpec(
            endpoint_selector=CiliumClusterwideNetworkPolicySpecEndpointSelector(
                match_expressions=[
                    CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressions(
                        key="k8s:io.kubernetes.pod.namespace",
                        operator=CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressionsOperator.IN,
                        values=[NAMESPACE],
                    )
                ]
            ),
            egress=[
                # DNS resolution (CoreDNS in kube-system)
                CiliumClusterwideNetworkPolicySpecEgress(
                    to_endpoints=[
                        CiliumClusterwideNetworkPolicySpecEgressToEndpoints(match_labels=cilium.KUBE_DNS_LABELS)
                    ],
                    to_ports=[
                        CiliumClusterwideNetworkPolicySpecEgressToPorts(
                            ports=[
                                *ports(53, protocol=CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol.UDP),
                                *ports(53),
                            ]
                        )
                    ],
                ),
                # All cluster-internal traffic (bypasses the proxy via NO_PROXY). This is how the
                # runner reaches the in-cluster Forgejo git + OCI registry (forgejo-http.forgejo:3000)
                # it clones source from, pushes images to, and long-polls for jobs, plus the
                # oci-cache Zot mirror for dind base-image pulls.
                # GOTCHA: Cilium's socket-LB translates a ClusterIP:port to backend podIP:targetPort
                # *before* egress policy is enforced, so the port here must be the backend
                # targetPort, not the Service port. oci-cache's Service is :80 but its pods listen on
                # 5000 -- hence 5000, not 80, is what unblocks dind->oci-cache.
                CiliumClusterwideNetworkPolicySpecEgress(
                    to_entities=[CiliumClusterwideNetworkPolicySpecEgressToEntities.CLUSTER],
                    to_ports=[CiliumClusterwideNetworkPolicySpecEgressToPorts(ports=ports(80, 443, 3000, 5000))],
                ),
                # haku-egress-proxy -- all external internet traffic must go through here.
                CiliumClusterwideNetworkPolicySpecEgress(
                    to_endpoints=[
                        CiliumClusterwideNetworkPolicySpecEgressToEndpoints(
                            match_labels={
                                "k8s:io.kubernetes.pod.namespace": "haku-egress-proxy",
                                "k8s:app.kubernetes.io/name": "haku-egress-proxy",
                            }
                        )
                    ],
                    to_ports=[CiliumClusterwideNetworkPolicySpecEgressToPorts(ports=ports(8080))],
                ),
            ],
        ),
    )


def _dind() -> keda.ScaledJobSpecJobTargetRefTemplateSpecInitContainers:
    """dind is a NATIVE SIDECAR (an initContainer with restartPolicy: Always), not an ordinary
    container. Two things depend on that, and both are load-bearing:

    1. TERMINATION ORDER. Ordinary containers are SIGTERMed in parallel, so dockerd died alongside
       the runner and took the build containers with it -- which is why
       `shutdown_timeout: 30m` + a 2100s grace period did NOT save the reaped builds they were
       added for: the runner waited gracefully on a job whose containers no longer existed.
       Native sidecars are terminated only AFTER the main container exits.
    2. JOB COMPLETION. In a Job, an ordinary dind container would never exit, so the Job would
       never reach Complete. A native sidecar is torn down by the kubelet when `runner` finishes.
       Without this the ScaledJob does not work at all.

    The startupProbe gates the runner on dockerd actually listening -- under a Deployment a
    too-early runner just crash-looped until dind was up, but with restartPolicy: Never that first
    crash would fail the whole Job.

    privileged: true is the documented requirement for docker:dind-rootless -- it provides /dev
    (incl. /dev/net/tun for RootlessKit) and disables the mount masks that otherwise hide those
    devices from RootlessKit's nested namespaces. The non-privileged path cleared seccomp (PSA) +
    user.max_user_namespaces (the OVH worker sysctl) but still couldn't create the rootless TAP
    because of the masks. Crucially the DAEMON still runs ROOTLESS (UID 1000 via the -rootless
    image), so this is strictly better than classic rootful dind; the privileged POD is the price,
    contained by haku-ci being operator-only (no Haku RBAC), off control planes, and
    egress-fenced. See cluster/k8s/haku-ci/README.md.
    """
    env = keda.ScaledJobSpecJobTargetRefTemplateSpecInitContainersEnv
    quantity = keda.ScaledJobSpecJobTargetRefTemplateSpecInitContainersResourcesRequests
    limit = keda.ScaledJobSpecJobTargetRefTemplateSpecInitContainersResourcesLimits
    mount = keda.ScaledJobSpecJobTargetRefTemplateSpecInitContainersVolumeMounts
    return keda.ScaledJobSpecJobTargetRefTemplateSpecInitContainers(
        name="dind",
        image="docker:29-dind-rootless",
        restart_policy="Always",
        args=[
            f"--host=tcp://0.0.0.0:{_DOCKER_PORT}",
            "--tls=false",
            # Pull Docker Hub base images through the in-cluster oci-cache Zot mirror instead of
            # docker.io (+ its CDN fronts) directly -- that's why the Docker Hub FQDNs are gone from
            # the haku-egress-proxy allowlist. The mirror serves Docker Hub at its root and fetches
            # from origin under oci-cache's own egress. `.svc.cluster.local` so NO_PROXY routes it
            # direct (not via the proxy); insecure-registry because oci-cache is plain HTTP on :80.
            # Classic dockerd only mirrors Docker Hub -- ghcr/quay still go direct via the proxy.
            "--registry-mirror=http://oci-cache.oci-cache.svc.cluster.local",
            "--insecure-registry=oci-cache.oci-cache.svc.cluster.local",
            # RootlessKit's slirp4netns forwards container DNS queries to CoreDNS as-is, and the
            # resolv.conf it gives dockerd carries no search list, so without this a container
            # cannot resolve a `<service>.<namespace>` name -- among them `forgejo-http.forgejo`,
            # the runner's registered address, which job checkouts use as `github.server_url`.
            "--dns-search=svc.cluster.local",
        ],
        env=[
            # --tls=false alone isn't enough: the dind entrypoint defaults DOCKER_TLS_CERTDIR to
            # /certs and re-enables TLS on 2375, so the runner's plain-HTTP client hits "HTTP
            # request to an HTTPS server". Emptying it disables TLS so 2375 is plain HTTP
            # (localhost-only inside the pod). This is the last piece that lets the runner connect
            # to the rootless daemon.
            env(name="DOCKER_TLS_CERTDIR", value=""),
            # dockerd pulls base images through haku-egress-proxy. Docker >=23 reads these proxy
            # vars from the daemon environment for registry pulls; NO_PROXY keeps the in-cluster
            # Forgejo registry push and public cluster Gateway names direct. dockerd is Go, so
            # SSL_CERT_FILE points its registry-TLS trust at the haku-egress-proxy CA bundle (a
            # full bundle incl. system + cluster roots), so it validates the intercepted registry
            # TLS.
            env(name="HTTP_PROXY", value=_PROXY_URL),
            env(name="HTTPS_PROXY", value=_PROXY_URL),
            env(name="NO_PROXY", value=_NO_PROXY),
            env(name="SSL_CERT_FILE", value=_CA_FILE),
        ],
        startup_probe=keda.ScaledJobSpecJobTargetRefTemplateSpecInitContainersStartupProbe(
            tcp_socket=keda.ScaledJobSpecJobTargetRefTemplateSpecInitContainersStartupProbeTcpSocket(
                port=keda.ScaledJobSpecJobTargetRefTemplateSpecInitContainersStartupProbeTcpSocketPort.from_number(
                    _DOCKER_PORT
                )
            ),
            period_seconds=2,
            failure_threshold=90,
        ),
        security_context=keda.ScaledJobSpecJobTargetRefTemplateSpecInitContainersSecurityContext(
            privileged=True, run_as_user=1000, run_as_group=1000
        ),
        volume_mounts=[
            mount(name="docker-data", mount_path="/var/lib/docker"),
            mount(name="egress-proxy-ca", mount_path="/egress-proxy-ca", read_only=True),
            # dind resolves the `-v /bazel-cache:/root/.cache` job-container bind mount (config.yaml)
            # against its own filesystem, so the pod-local emptyDir must be mounted here rather
            # than just in the runner.
            mount(name="bazel-cache", mount_path="/bazel-cache"),
        ],
        resources=keda.ScaledJobSpecJobTargetRefTemplateSpecInitContainersResources(
            requests={"cpu": quantity.from_string("200m"), "memory": quantity.from_string("512Mi")},
            limits={"cpu": limit.from_string("3"), "memory": limit.from_string("6Gi")},
        ),
    )


def _runner() -> keda.ScaledJobSpecJobTargetRefTemplateSpecContainers:
    """The Forgejo Actions runner: registers EPHEMERALLY with the haku-state repo, waits for exactly
    one job, runs it in a docker-cli job container against the dind sidecar, and exits -- which
    completes the Job and ends the pod."""
    env = keda.ScaledJobSpecJobTargetRefTemplateSpecContainersEnv
    quantity = keda.ScaledJobSpecJobTargetRefTemplateSpecContainersResourcesRequests
    limit = keda.ScaledJobSpecJobTargetRefTemplateSpecContainersResourcesLimits
    mount = keda.ScaledJobSpecJobTargetRefTemplateSpecContainersVolumeMounts
    return keda.ScaledJobSpecJobTargetRefTemplateSpecContainers(
        name="runner",
        # Pinned, and a major jump from the old floating `:6` tag -- unavoidable, since `one-job`
        # needs runner >6.1 and `register --ephemeral` needs both a recent runner and a Forgejo 15+
        # server (this instance reports 15.0.3). 12.13.2 rather than the newest 13.0.0: it carries
        # `--ephemeral`, `one-job` and `--wait` (verified in its source tree) with several more
        # weeks of soak. Pinned rather than floating so an upstream release can't silently change
        # CI behaviour.
        # Crossing 8.0.0 means workflows are now schema-validated and will REFUSE to run if they
        # don't parse -- see README, "Upgrading the runner image".
        image="code.forgejo.org/forgejo/runner:12.13.2",
        command=["sh", "-c"],
        # --ephemeral tells Forgejo to DELETE this runner registration once it has run one job. The
        # old Deployment re-registered on every pod start and never deregistered -- its comment
        # claimed "ephemeral" but the flag was absent, and the repo had accumulated 529 runner
        # registrations, 525 of them offline. --ephemeral is refused outright by servers older
        # than Forgejo 15, so this fails loudly rather than drifting silently.
        #
        # `one-job --wait` blocks until Forgejo assigns a task, runs it, and exits. The --wait
        # matters: without it the runner makes a single fetch attempt and exits NON-ZERO if the
        # task isn't dispatchable in that instant, which would fail the Job on a pure startup
        # race. Bounded by activeDeadlineSeconds.
        args=[
            "set -e\n"
            "forgejo-runner register --no-interactive --ephemeral \\\n"
            '  --instance "$FORGEJO_INSTANCE_URL" \\\n'
            '  --token "$(cat /secrets/token)" \\\n'
            '  --name "$RUNNER_NAME" \\\n'
            f'  --labels "{_JOB_LABEL}" \\\n'
            "  --config /config/config.yaml\n"
            "exec forgejo-runner one-job --wait --config /config/config.yaml\n"
        ],
        env=[
            env(name="FORGEJO_INSTANCE_URL", value=_FORGEJO_URL),
            # Every pod registers separately. A fixed name would race or overwrite registrations
            # when KEDA starts additional pods.
            env(
                name="RUNNER_NAME",
                value_from=keda.ScaledJobSpecJobTargetRefTemplateSpecContainersEnvValueFrom(
                    field_ref=keda.ScaledJobSpecJobTargetRefTemplateSpecContainersEnvValueFromFieldRef(
                        field_path="metadata.name"
                    )
                ),
            ),
            env(name="DOCKER_HOST", value=f"tcp://localhost:{_DOCKER_PORT}"),
            # Route the runner's external egress (fetching `uses:` actions from data.forgejo.org,
            # etc.) through haku-egress-proxy -- the sole external door (the force-proxy CCNP
            # above). NO_PROXY keeps in-cluster Forgejo (git + registry, long-poll), public cluster
            # Gateway names (*.allegedly.works), and the localhost dind socket direct. The public
            # names still land on Cilium Gateway nodes and stay bounded by the cluster-only CNP;
            # sending them through haku-egress-proxy creates a DNS-round-robin chance of proxy-pod
            # -> own-node Gateway self-hairpin, which Cilium Gateway answers with Envoy "Access
            # denied" for this policy-constrained identity. Include both wildcard and suffix forms
            # because NO_PROXY matching differs by client.
            env(name="HTTP_PROXY", value=_PROXY_URL),
            env(name="HTTPS_PROXY", value=_PROXY_URL),
            env(name="NO_PROXY", value=_NO_PROXY),
            *(
                env(name=name, value=_CA_FILE)
                for name in (
                    "SSL_CERT_FILE",
                    "CURL_CA_BUNDLE",
                    "GIT_SSL_CAINFO",
                    "REQUESTS_CA_BUNDLE",
                    "NODE_EXTRA_CA_CERTS",
                )
            ),
        ],
        volume_mounts=[
            mount(name="config", mount_path="/config", read_only=True),
            mount(name="token", mount_path="/secrets", read_only=True),
            mount(name="runner-data", mount_path="/data"),
            mount(name="egress-proxy-ca", mount_path="/egress-proxy-ca", read_only=True),
        ],
        resources=keda.ScaledJobSpecJobTargetRefTemplateSpecContainersResources(
            requests={"cpu": quantity.from_string("50m"), "memory": quantity.from_string("128Mi")},
            limits={"cpu": limit.from_string("1"), "memory": limit.from_string("1Gi")},
        ),
    )


def _volumes() -> list[keda.ScaledJobSpecJobTargetRefTemplateSpecVolumes]:
    volume = keda.ScaledJobSpecJobTargetRefTemplateSpecVolumes
    empty_dir = keda.ScaledJobSpecJobTargetRefTemplateSpecVolumesEmptyDir
    return [
        volume(
            name="config",
            config_map=keda.ScaledJobSpecJobTargetRefTemplateSpecVolumesConfigMap(name="haku-runner-config"),
        ),
        volume(
            name="token",
            # Provisioned out-of-band during paving (see README): a repo-scoped runner
            # registration token for haku-state. The pod stays pending until this Secret exists.
            secret=keda.ScaledJobSpecJobTargetRefTemplateSpecVolumesSecret(secret_name="haku-ci-runner-token"),
        ),
        # Holds the `.runner` file written by `register` (config.yaml pins the path).
        volume(name="runner-data", empty_dir=empty_dir()),
        volume(name="docker-data", empty_dir=empty_dir()),
        # Bazel cache (output base + --disk_cache + repo cache), bind-mounted into each job
        # container's ~/.cache. Under the old Deployment it also warmed SUCCESSIVE jobs on a
        # surviving pod; one job per pod means it now only spans the Bazel invocations WITHIN a
        # single CI job (bazel-ci's `Test` step then `Build`), which is still most of the win. See
        # README for the cost and the follow-up.
        volume(name="bazel-cache", empty_dir=empty_dir()),
        # haku-egress-proxy CA trust bundle, written into this namespace by the trust-manager
        # Bundle (agents/haku-egress-proxy/trust-bundle.yaml). Mounted into both the runner and
        # dind so intercepted external TLS validates.
        volume(
            name="egress-proxy-ca",
            config_map=keda.ScaledJobSpecJobTargetRefTemplateSpecVolumesConfigMap(name="haku-egress-proxy-ca-cert"),
        ),
    ]


def _add_runner(chart: Chart) -> None:
    # Forgejo's /metrics endpoint exposes no Actions queue-depth metric. KEDA's native
    # forgejo-runner scaler instead polls Forgejo's authenticated, repo-scoped runner-jobs endpoint,
    # filtered to this runner label. Its result is exactly the number of jobs presently waiting for
    # a haku-ci runner.
    TriggerAuthentication(
        chart,
        "trigger-authentication",
        metadata=metadata(_AUTH, NAMESPACE),
        spec=TriggerAuthenticationSpec(
            secret_target_ref=[
                TriggerAuthenticationSpecSecretTargetRef(
                    parameter="token", name=FORGEJO_TOKEN_SECRET, key=FORGEJO_TOKEN_KEY
                )
            ]
        ),
    )
    # ScaledJob, NOT ScaledObject -- this is the whole point.
    #
    # The forgejo-runner trigger counts QUEUED jobs. Under a ScaledObject that metric drives an HPA
    # over a long-lived Deployment, and the same number that scales up also scales down: the moment
    # a runner picks a job up the metric returns to zero, the HPA concludes the pods are idle, and
    # it deletes one at random. It has no way to know which pod is mid-build. That reaped builds on
    # `haku-state` repeatedly (see README, "Queue autoscaling").
    #
    # Under a ScaledJob the same metric is only ever a CREATE signal. KEDA translates "N jobs
    # queued" into "create N Kubernetes Jobs", never deletes a running one, and each pod ends its
    # own life when its single CI job is done. There is no scale-down path to get wrong, so the
    # failure mode is structurally absent rather than mitigated. This is also the shape the scaler
    # is documented for: https://keda.sh/docs/2.20/scalers/forgejo/
    keda.ScaledJob(
        chart,
        "scaled-job",
        metadata=metadata(_RUNNER, NAMESPACE, labels=_LABELS),
        spec=keda.ScaledJobSpec(
            # One pod per queued job, up to four concurrently. No minimum: between bursts there are
            # no runner pods at all, which was already true under the ScaledObject
            # (minReplicaCount: 0) -- the runner holds no state worth keeping warm.
            max_replica_count=4,
            polling_interval=15,
            successful_jobs_history_limit=3,
            # Keep more failures than successes: a failed pod's logs are the only forensics for an
            # infrastructure fault (registration refused, dind never came up), since a failed
            # *build* exits 0 here -- see `backoff_limit` below.
            failed_jobs_history_limit=5,
            # gradual = editing this ScaledJob does NOT delete Jobs already running. The default
            # ("immediate") would kill in-flight builds on every Flux reconcile that touches this
            # object, reintroducing the exact bug this migration removes, just with a different
            # trigger.
            rollout=keda.ScaledJobSpecRollout(strategy=keda.ScaledJobSpecRolloutStrategy.GRADUAL),
            job_target_ref=keda.ScaledJobSpecJobTargetRef(
                parallelism=1,
                completions=1,
                # No Kubernetes-level retry. A failed *build* is not a failed Job -- the runner
                # reports the failure to Forgejo and exits 0 -- so a non-zero exit here means an
                # infrastructure fault, and retrying it in-place would just fail the same way. The
                # correct retry already exists: the CI job stays queued, so the next KEDA poll
                # creates a fresh pod.
                backoff_limit=0,
                # Upper bound on the pod's whole life: waiting for a task + running it.
                # `one-job --wait` blocks until Forgejo hands it a task, so a pod created for a job
                # that is cancelled before pickup would otherwise wait forever holding one of the
                # four slots. 70m = config.yaml's `timeout: 1h` plus slack.
                active_deadline_seconds=4200,
                template=keda.ScaledJobSpecJobTargetRefTemplate(
                    metadata=keda.ScaledJobSpecJobTargetRefTemplateMetadata(labels=_LABELS),
                    spec=keda.ScaledJobSpecJobTargetRefTemplateSpec(
                        restart_policy="Never",
                        automount_service_account_token=False,
                        # Only matters for eviction/drain now -- nothing deletes this pod mid-build
                        # any more. Slightly above config.yaml's `shutdown_timeout: 30m` so a
                        # drained node lets a build of up to that length finish instead of dropping
                        # it.
                        termination_grace_period_seconds=1830,
                        # No node affinity: this privileged, agent-controlled build compute may land
                        # on any worker -- the HIL workers, the home OptiPlex, wyrm2, and the roaming
                        # laptops when they are reachable. Control-plane nodes keep their NoSchedule
                        # taint and stay out. A Talos worker needs `user.max_user_namespaces` raised
                        # for rootless dind (ovh-nodes.tf, home-nodes.tf); the NixOS hosts keep the
                        # kernel default.
                        tolerations=[
                            # Roaming laptops (iguana, rugged) are tainted so ordinary workloads avoid
                            # them; a CI job is disposable enough to run there. If the laptop leaves
                            # mid-build the unreachable taint evicts the pod and the queued job is
                            # retried on the next KEDA poll, at the cost of the minutes already spent.
                            keda.ScaledJobSpecJobTargetRefTemplateSpecTolerations(
                                key="node-role.kubernetes.io/roaming",
                                operator="Equal",
                                value="true",
                                effect="NoSchedule",
                            )
                        ],
                        # Requests describe only the runner's idle footprint, so the default
                        # scheduler's resource scoring alone can co-locate an entire four-job burst.
                        # Prefer an even host spread, but keep CI available when only one capable
                        # worker is schedulable.
                        topology_spread_constraints=[
                            keda.ScaledJobSpecJobTargetRefTemplateSpecTopologySpreadConstraints(
                                max_skew=1,
                                topology_key="kubernetes.io/hostname",
                                when_unsatisfiable="ScheduleAnyway",
                                label_selector=keda.ScaledJobSpecJobTargetRefTemplateSpecTopologySpreadConstraintsLabelSelector(
                                    match_labels=_LABELS
                                ),
                            )
                        ],
                        init_containers=[_dind()],
                        containers=[_runner()],
                        volumes=_volumes(),
                    ),
                ),
            ),
            triggers=[
                keda.ScaledJobSpecTriggers(
                    type="forgejo-runner",
                    # No `name:` -- the docs list it as required, but the scaler filters on labels
                    # and the current deployment has worked without it. A fixed name could not
                    # match anyway: every pod registers under its own pod name.
                    metadata={"address": _FORGEJO_URL, "owner": "haku", "repo": "haku-state", "labels": "haku-ci"},
                    authentication_ref=keda.ScaledJobSpecTriggersAuthenticationRef(name=_AUTH),
                )
            ],
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # Operator-owned CI namespace for Haku's image builds. Deliberately NOT haku-sandbox -- Haku
    # has no RBAC here, so a prompt-injected Haku cannot tamper with the runner pod or its
    # registry/git push creds. But the runner executes Haku-authored build steps (its workflow +
    # Dockerfile), so it IS agent-controlled compute and is egress-fenced like haku-sandbox. See
    # cluster/k8s/haku-ci/README.md.
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={
                "name": NAMESPACE,
                "rbac.ducktape.io/agent-readable-logs": "true",
                # The runner's resources are set deliberately; no VPA recommendations wanted.
                "goldilocks.fairwinds.com/enabled": "false",
                # Enforce the privileged Pod Security level. The dind sidecar runs privileged (the
                # documented requirement for docker:dind-rootless -- it provides /dev/net/tun and
                # disables the mount masks RootlessKit needs; the daemon itself still runs rootless
                # as UID 1000). baseline/restricted forbid both privileged and its Unconfined
                # seccomp, so the namespace must enforce privileged. Safe because haku-ci is
                # operator-only (Haku has no RBAC here; only Flux applies), so the loosened level
                # grants Haku nothing.
                "pod-security.kubernetes.io/enforce": "privileged",
                "pod-security.kubernetes.io/warn": "privileged",
                "pod-security.kubernetes.io/audit": "privileged",
            },
        ),
    )
    _add_egress_fence(chart)
    _add_runner(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
