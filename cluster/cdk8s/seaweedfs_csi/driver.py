"""The SeaweedFS CSI driver, pinned to OVH nodes, with the GitRepository its chart comes
from, its StorageClasses, and the directory's Flux Kustomization.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from flux_gitrepository_crds.io.fluxcd.toolkit.source import GitRepository, GitRepositorySpec, GitRepositorySpecRef
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeRemediation,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "seaweedfs-csi"
NAMESPACE = "seaweedfs-csi-system"
RELEASE = "seaweedfs-csi-driver"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/seaweedfs-csi"
_VERSION = "v1.4.30"
_ZONE = "topology.kubernetes.io/zone"
_OVH_AFFINITY = {
    "nodeAffinity": {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": [{"key": _ZONE, "operator": "In", "values": ["hil-ovh"]}]}]
        }
    }
}
# OVH control-plane nodes carry the control-plane NoSchedule taint, and the node affinity
# above admits them (they are hil-ovh). Without a matching toleration, a control-plane node
# that serves SeaweedFS cannot mount its PVCs: the volume server tolerates the taint, yet the
# node's CSINode may not advertise the seaweedfs-csi-driver, so a tolerating pod can hang in
# ContainerCreating.
# Not an etcd-contention risk -- etcd rides the install disk and SeaweedFS the separate data
# disk, which is why <cluster/docs/lessons_learned/2026_06_19_etcd_hdd_io_contention.md> kept
# SeaweedFS on /dev/sdb and pinned only ephemeral-write batch jobs off the control planes.
# Measured on ovh-ns103656: the mount pod writes ~0.8 KiB/s to the install disk (6.0 GiB over
# 85 days, logs + FUSE metadata) against the 490-988 KiB/s writers that RCA actually pinned off.
_CONTROL_PLANE_TOLERATIONS = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]


def _values() -> dict[str, object]:
    return {
        "seaweedfsFiler": "seaweedfs-filer.seaweedfs.svc.cluster.local:8888",
        # Chart-created StorageClass disabled (storageClassName left empty): the chart's
        # storageclass.yaml template has no allowedTopologies support (only
        # name/provisioner/volumeBindingMode/parameters), so the default `seaweedfs-ovh` class
        # is built here instead, alongside `seaweedfs-ovh-ssd` -- same name, same
        # provisioner/volumeBindingMode as the chart would have produced, plus
        # allowedTopologies.
        "storageClassName": "",
        # Node label keys the driver reports as each node's CSI accessible_topology
        # (NodeGetInfo) and that csi-provisioner requires to honour StorageClass
        # allowedTopologies (--feature-gates=Topology=true, gated on this being non-empty).
        # Picks up seaweedfs/seaweedfs-csi-driver#301 (merged to master 2026-08-26, released
        # v1.4.30): before this, NodeGetInfo reported no topology at all and CSINode showed
        # topologyKeys=null for every node, so nothing stopped the scheduler from placing a
        # seaweedfs-ovh*-PVC pod on a non-OVH node (cluster/k8s/kyverno/policies/ formerly
        # carried a pin-seaweedfs-ovh-consumers ClusterPolicy working around exactly this, now
        # removed in favor of the StorageClasses' own allowedTopologies).
        # `topology.kubernetes.io/zone` matches the node label OVH nodes actually carry
        # (cluster/terraform/main/ovh-nodes.tf nodeLabels, value "hil-ovh") and what the
        # StorageClasses' allowedTopologies match on.
        "topologyKeys": [_ZONE],
        # Bound the per-mount in-memory write buffer. `weed mount` accumulates dirty pages as
        # chunks of -chunkSizeLimitMB (2MB) and allocates a new in-memory one only while
        # `memChunkCounter < 4*writableChunkLimit` (weed/mount/page_writer/upload_pipeline.go),
        # where writableChunkLimit is exactly this value; past that, chunks spill to the
        # on-disk swap file under -cacheDir. So the ceiling is 4 x concurrentWriters x 2MB *per
        # weed-mount process*, and the mount pod runs one such process per PVC on the node --
        # at the default 128 that is 1GiB each, unbounded in aggregate.
        #
        # 64 -> 512MiB/process. The busiest mount observed held ~245Mi of Go heap (~122
        # chunks); 64 allows 256 chunks, ~2x that. Dropping to 32 would cap at 128 chunks,
        # i.e. right at the observed working set, pushing a normally-loaded mount onto the
        # disk swap path for no benefit.
        #
        # The knob that would bound this properly is `weed mount -writeBufferSizeMB` (a real
        # accountant with a disk evictor, weed/mount/weedfs.go). The CSI driver does not pass
        # it -- it is absent from buildMountArgs' argsMap on v1.4.30, so it is unreachable
        # without an upstream change.
        #
        # NOTE: GOMEMLIMIT is not an alternative here. The env var is inherited by every
        # forked weed-mount child and applied per process, so a pod-level budget divided by a
        # child count that varies with PVC scheduling has no correct static value.
        #
        # Rollout: this is a top-level chart value and lands as --concurrentWriters on the
        # *node* DaemonSet (templates/daemonset.yaml), which is RollingUpdate -- so it rolls
        # on apply. That is safe: the node plugin serves CSI RPCs and holds no FUSE mounts
        # (those are in the mount DaemonSet), so the only window is a few seconds per node
        # where NodeStage/NodePublish is unavailable and a starting pod retries. The node
        # plugin builds the mount args and sends them to the mount service per volume, so the
        # new value applies to newly staged volumes; volumes already mounted keep 128 until
        # their weed-mount process restarts.
        "concurrentWriters": 64,
        # Keep the image tags explicit and in sync with the source tag so the deployed plugin
        # and mount-service versions are auditable.
        "seaweedfsCsiPlugin": {"image": "chrislusf/seaweedfs-csi-driver", "tag": _VERSION},
        # Restrict the FUSE mount service + CSI node daemons to OVH kimsufi nodes. Pods
        # consuming seaweedfs-ovh PVCs must schedule on OVH; we don't want phantom daemonsets
        # on non-OVH nodes until we know the backend is suitable.
        "mountService": {
            "enabled": True,
            "image": "chrislusf/seaweedfs-mount",
            "tag": _VERSION,
            # QoS / eviction protection. Without requests this pod is BestEffort: the
            # kubelet's first eviction target and the kernel OOM-killer's first victim (max
            # oom_score_adj) under node memory pressure. It holds the weed-mount FUSE
            # processes for every SeaweedFS volume on the node, so its death is catastrophic
            # and silent -- consumers keep a dead mount ("transport endpoint is not
            # connected") while their own readiness stays green. That is exactly how it died
            # on 2026-08-23, breaking ~half of all git for ~14.5h (agentydragon/ducktape#4616).
            # No CPU limit (never throttle the live data path). priorityClassName
            # (system-node-critical, chart default) already shields from preemption and
            # defers eviction in the priority sort.
            #
            # Sized on measured usage, not estimate. A pod's footprint scales with the number
            # of PVCs the scheduler put on its node (one weed-mount process each), so the
            # busiest node is the one that matters -- 6 mounts on ovh-ns104952, over 30d: p50
            # 434Mi, p90 607Mi, p99 705Mi, max 1728Mi.
            #   - request 640Mi: above that p90, so kubelet node-pressure eviction ranks it
            #     last. (The previous 384Mi predates this measurement and was below the
            #     busiest node's *median*.)
            #   - limit 2Gi: the observed 1728Mi peak was taken under the old
            #     concurrentWriters=128 ceiling, so halving that should keep a 6-mount node
            #     near ~1.2GiB -- but that is an estimate, and a limit reserves nothing. For a
            #     pod whose death silently breaks every volume on the node, headroom is the
            #     right trade.
            "resources": {"requests": {"cpu": "100m", "memory": "640Mi"}, "limits": {"memory": "2Gi"}},
            "affinity": _OVH_AFFINITY,
            "tolerations": _CONTROL_PLANE_TOLERATIONS,
        },
        "node": {
            "enabled": True,
            # BestEffort -> Burstable so the CSI node plugin (NodeStage/NodePublish, the path
            # that mounts volumes into pods) isn't an early eviction/OOM target either. Steady
            # state ~40Mi; see mountService above and #4616.
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}},
            "affinity": _OVH_AFFINITY,
            "tolerations": _CONTROL_PLANE_TOLERATIONS,
        },
        "controller": {
            "replicas": 1,
            # BestEffort -> Burstable; provisioning/attach path (not the live data path, but
            # no reason to leave it first-to-evict). Steady state ~85Mi.
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}},
            "affinity": _OVH_AFFINITY,
            "tolerations": _CONTROL_PLANE_TOLERATIONS,
        },
    }


def _storage_class(scope: Construct, name: str, *, description: str, parameters: dict[str, str] | None = None) -> None:
    k8s.KubeStorageClass(
        scope,
        name,
        metadata=k8s.ObjectMeta(name=name, annotations={"description": description}),
        provisioner=RELEASE,
        reclaim_policy="Delete",
        volume_binding_mode="WaitForFirstConsumer",
        # The driver's node/mount/controller plugins run only on hil-ovh nodes (the release's
        # node affinity) and, since v1.4.30, report that zone as their CSI
        # accessible_topology (`topologyKeys`). This makes the scheduler's
        # dynamic-provisioning volume binding actually enforce placement onto a node with the
        # driver present, matching the value OVH nodes carry on this label
        # (cluster/terraform/main/ovh-nodes.tf nodeLabels).
        allowed_topologies=[
            k8s.TopologySelectorTerm(
                match_label_expressions=[k8s.TopologySelectorLabelRequirement(key=_ZONE, values=["hil-ovh"])]
            )
        ],
        # SeaweedFS CSI expansion is a collection-quota bump, so a growing PVC (e.g. Forgejo
        # git) can be resized in place.
        allow_volume_expansion=True,
        parameters=parameters,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            # CSI driver needs hostPath, hostPID, SYS_ADMIN, and privileged containers.
            labels={
                "pod-security.kubernetes.io/enforce": "privileged",
                "pod-security.kubernetes.io/audit": "privileged",
                "pod-security.kubernetes.io/warn": "privileged",
            },
        ),
    )
    source = GitRepository(
        chart,
        "source",
        metadata=metadata("seaweedfs-csi-driver", "flux-system"),
        spec=GitRepositorySpec(
            interval="24h",
            url="https://github.com/seaweedfs/seaweedfs-csi-driver",
            ref=GitRepositorySpecRef(tag="v1.4.31"),
            ignore="/*\n!/deploy/helm/seaweedfs-csi-driver\n",
        ),
    )
    HelmRelease(
        chart,
        "release",
        metadata=metadata(RELEASE, NAMESPACE),
        spec=HelmReleaseSpec(
            interval="30m",
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
            upgrade=HelmReleaseSpecUpgrade(
                # Deviation: upgrades do not wait for resource health. The mount DaemonSet is
                # `updateStrategy: OnDelete` (chart default, and deliberate -- rolling it kills
                # the node's FUSE mounts and consumers do not self-heal, #4616). Flux assesses
                # health with kstatus, which reports a DaemonSet InProgress while
                # updatedNumberScheduled < desiredNumberScheduled and has no OnDelete
                # exemption, so any values change here hangs until the timeout and then Stalls
                # the release -- which also wedges the Kustomization that health-checks it.
                # (Helm's own readiness checker does exempt OnDelete; Flux does not use it.)
                # Adding resource requests in #4626 triggered exactly this.
                #
                # The cost is that a genuinely broken upgrade of the controller or node
                # DaemonSet is no longer caught by the release going NotReady. Reverting this
                # requires either dropping OnDelete or a per-resource wait exemption.
                disable_wait=True,
                remediation=HelmReleaseSpecUpgradeRemediation(retries=3),
            ),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart="deploy/helm/seaweedfs-csi-driver",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.GIT_REPOSITORY,
                        name=source.name,
                        namespace=source.metadata.namespace,
                    ),
                )
            ),
            values=_values(),
        ),
    )
    _storage_class(
        chart,
        "seaweedfs-ovh",
        description=(
            "SeaweedFS CSI (RWX, distributed), media-blind (HDD bulk). Default for app data volumes. "
            "See sc-seaweedfs-ovh-ssd.yaml for the SSD-pinned tier."
        ),
    )
    _storage_class(
        chart,
        "seaweedfs-ovh-ssd",
        description=(
            "SeaweedFS CSI (RWX, distributed) pinned to the SSD volume tier. diskType=ssd maps to "
            "`weed mount -disk=ssd`, so volumes land only on ssd-tagged volume servers (the OVH NVMe "
            "topology group). Reserved for fsync/latency-critical RWX data (Forgejo git). The default "
            "seaweedfs-ovh class is media-blind (HDD bulk). See cluster/docs/plans/ovh_storage_tiering.md."
        ),
        # CSI v1.4.30 maps `diskType` to `weed mount -disk` and passes `replication` through
        # (verified against pkg/driver/mounter.go).
        parameters={"diskType": "ssd", "replication": "001"},
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def seaweedfs_csi(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, seaweedfs_cluster: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart, NAME, artifact, timeout="10m", depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)]
    )
