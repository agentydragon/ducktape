"""The in-cluster NixOS qcow2 image publisher (README.md beside the output): its privileged
namespace, the suspended CronJob operators create one-off Jobs from, and the `vm-images`
bucket with its writer and CDI reader credentials.

Hand-written beside the output: `publish.sh` (the directory's `kustomization.yaml`
generates the scripts ConfigMap from it) and `attic-reader-netrc.sops.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct

from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.seaweedfs import s3

NAME = "vm-images-publisher"
OUTPUT_DIR = "cluster/k8s/vm-images-publisher"
_BUCKET = "vm-images"
_WRITER = "vm-images-ci-writer"
_READER = "vm-images-cdi-reader"


def _credentials_secret(identity: str) -> str:
    return f"{identity}-s3-credentials"


def _identity(scope: Construct, name: str) -> s3.Identity:
    """A cluster-global S3Identity plus the publisher-local S3Credentials the operator mints
    its key pair into (Secret `<name>-s3-credentials` in this namespace)."""
    identity = s3.Identity(scope, name, name=name)
    identity.credentials(namespace=NAME, secret=_credentials_secret(name), key_fields=None)
    return identity


def _storage(scope: Construct) -> None:
    bucket = s3.Bucket(
        scope,
        "bucket",
        name=_BUCKET,
        namespace=NAME,
        # The physical bucket already exists; this CR is moving to the publisher's
        # namespace without deleting or recreating its data.
        adopt_existing=True,
    )
    bucket.grant_read_write(_identity(scope, _WRITER))
    bucket.grant_read(_identity(scope, _READER))


def _writer_env(name: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name,
        value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=_credentials_secret(_WRITER), key=key)),
    )


def _cron_job(scope: Construct) -> None:
    k8s.KubeCronJob(
        scope,
        "cronjob",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAME,
            annotations={
                "description": (
                    "Builds `.#bootstrap-image` from a chosen git ref in-cluster and uploads the qcow2 to SeaweedFS"
                    " via the internal S3 endpoint. Suspended on purpose; operators invoke with `kubectl create job"
                    " --from=cronjob/vm-images-publisher publish-<sha> -n vm-images-publisher`. See README.md."
                )
            },
        ),
        spec=k8s.CronJobSpec(
            schedule="0 0 1 1 *",
            suspend=True,
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=3,
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    backoff_limit=0,
                    active_deadline_seconds=5400,
                    template=k8s.PodTemplateSpec(
                        spec=k8s.PodSpec(
                            restart_policy="Never",
                            # The internal SeaweedFS endpoint resolves via normal pod DNS -- no
                            # hostNetwork needed.
                            node_selector={"topology.kubernetes.io/region": "hil"},
                            # Keep batch writers off CPs; hard affinity guards future toleration
                            # changes.
                            affinity=k8s.Affinity(
                                node_affinity=k8s.NodeAffinity(
                                    required_during_scheduling_ignored_during_execution=k8s.NodeSelector(
                                        node_selector_terms=[
                                            k8s.NodeSelectorTerm(
                                                match_expressions=[
                                                    k8s.NodeSelectorRequirement(
                                                        key="node-role.kubernetes.io/control-plane",
                                                        operator="DoesNotExist",
                                                    )
                                                ]
                                            )
                                        ]
                                    )
                                )
                            ),
                            containers=[
                                k8s.Container(
                                    name="publish",
                                    image="nixos/nix:2.35.2",
                                    image_pull_policy="IfNotPresent",
                                    resources=k8s.ResourceRequirements(
                                        requests={
                                            "cpu": k8s.Quantity.from_string("2"),
                                            "memory": k8s.Quantity.from_string("4Gi"),
                                        },
                                        limits={
                                            "cpu": k8s.Quantity.from_string("4"),
                                            "memory": k8s.Quantity.from_string("8Gi"),
                                        },
                                    ),
                                    env=[
                                        # cache.allegedly.works/main lets the publisher substitute
                                        # host-image closures (e.g. sops-install-secrets, a sops-nix
                                        # package absent from cache.nixos.org) instead of building
                                        # them -- the build pod has no egress for go-module fetches.
                                        # nix-attic-push builds every flake output on devel, so the
                                        # agent-box-image closure is already cached. main pubkey SSOT:
                                        # nix/attic-pubkeys.json. Auth via the mounted netrc.
                                        k8s.EnvVar(
                                            name="NIX_CONFIG",
                                            value=(
                                                "experimental-features = nix-command flakes\n"
                                                "substituters = https://cache.nixos.org/"
                                                " https://cache.allegedly.works/main\n"
                                                "trusted-public-keys ="
                                                " cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
                                                " main:owYQITaq2ixR/EnqKoIQAxgjalKKVqMemFwRMaUW53U=\n"
                                                "netrc-file = /etc/nix-netrc/netrc\n"
                                                "system-features = benchmark big-parallel kvm nixos-test uid-range\n"
                                                "accept-flake-config = true\n"
                                            ),
                                        ),
                                        _writer_env("AWS_ACCESS_KEY_ID", "accessKey"),
                                        _writer_env("AWS_SECRET_ACCESS_KEY", "secretKey"),
                                    ],
                                    # nixos/nix has no /bin/sh and only a minimal profile (no git,
                                    # awscli2, gawk). Drop into a `nix shell` that provides the
                                    # script's runtime deps, then exec it. `nix shell` (unlike `nix
                                    # profile install`) doesn't read the existing user profile, so it
                                    # works under default pure-eval.
                                    command=[
                                        "bash",
                                        "-c",
                                        "exec nix shell nixpkgs#git nixpkgs#awscli2 nixpkgs#gawk"
                                        " --command bash /scripts/publish.sh",
                                    ],
                                    security_context=k8s.SecurityContext(privileged=True),
                                    volume_mounts=[
                                        k8s.VolumeMount(name="scripts", mount_path="/scripts"),
                                        k8s.VolumeMount(
                                            name="attic-netrc", mount_path="/etc/nix-netrc", read_only=True
                                        ),
                                        k8s.VolumeMount(name="kvm", mount_path="/dev/kvm"),
                                        k8s.VolumeMount(name="tmp", mount_path="/tmp"),
                                    ],
                                )
                            ],
                            volumes=[
                                k8s.Volume(
                                    name="scripts",
                                    # Generated from publish.sh by the directory's kustomization.yaml.
                                    config_map=k8s.ConfigMapVolumeSource(
                                        name="vm-images-publisher-scripts", default_mode=0o555
                                    ),
                                ),
                                k8s.Volume(
                                    name="attic-netrc",
                                    secret=k8s.SecretVolumeSource(
                                        secret_name="attic-reader-netrc",
                                        items=[k8s.KeyToPath(key="netrc", path="netrc")],
                                    ),
                                ),
                                k8s.Volume(
                                    name="kvm", host_path=k8s.HostPathVolumeSource(path="/dev/kvm", type="CharDevice")
                                ),
                                # No /nix mount: the image already populates /nix/store with bash,
                                # nix, coreutils, etc. An emptyDir here would shadow them and the
                                # container couldn't exec `bash`. Container overlay handles writes.
                                k8s.Volume(
                                    name="tmp",
                                    empty_dir=k8s.EmptyDirVolumeSource(size_limit=k8s.Quantity.from_string("8Gi")),
                                ),
                            ],
                        )
                    ),
                )
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAME,
            labels={
                "name": NAME,
                # The Job needs hostPath /dev/kvm + privileged for nix's `kvm` system feature
                # (qemu-efi image builder). Same constraint as the smoke Job under
                # x/kubevirt_nixos_bootstrap/.
                "pod-security.kubernetes.io/enforce": "privileged",
                "pod-security.kubernetes.io/audit": "privileged",
                "pod-security.kubernetes.io/warn": "privileged",
                # Events are included in namespace-diagnostics-reader and are safe for the
                # public-coder diagnostics surface.
                "rbac.ducktape.io/agent-readable-metadata": "true",
            },
        ),
    )
    _storage(chart)
    _cron_job(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
