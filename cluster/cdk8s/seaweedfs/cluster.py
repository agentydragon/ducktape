"""The SeaweedFS cluster on the OVH Kimsufi nodes: the `Seaweed` CR, its hourly
replication-repair `AdminScript`, and the PodDisruptionBudgets the operator CRD has no
field for.

Replication "001" (1 copy on a different node, same rack) survives a single node loss in
each 3-node, single-rack topology group. Volume server PVCs use the `local-path-ovh-*`
classes, bound to the Talos data user volume on each node. S3 runs as a standalone
deployment (`spec.s3`, not `filer.s3`); the filer stays because S3 needs it for metadata.
Every component is pinned to OVH nodes: the operator default would let pods land anywhere.

Hand-written beside the output: `filer.toml` and the directory's `kustomization.yaml`,
whose configMapGenerator + replacement fill `spec.filer.config` from it (the TOML lives
in its own file so it can be syntax-checked outside YAML string escaping). The
PriorityClass beside them is `stateful_infra`'s.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from seaweed_adminscript_crds.com.seaweedfs.seaweed import (
    AdminScript,
    AdminScriptSpec,
    AdminScriptSpecClusterRef,
    AdminScriptSpecConcurrencyPolicy,
    AdminScriptSpecResources,
    AdminScriptSpecResourcesLimits,
    AdminScriptSpecResourcesRequests,
    AdminScriptSpecRestartPolicy,
)
from seaweed_seaweed_crds.com.seaweedfs.seaweed import (
    Seaweed,
    SeaweedSpec,
    SeaweedSpecFiler,
    SeaweedSpecFilerAffinity,
    SeaweedSpecFilerAffinityNodeAffinity,
    SeaweedSpecFilerAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution,
    SeaweedSpecFilerAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference,
    SeaweedSpecFilerAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions,
    SeaweedSpecFilerAffinityPodAntiAffinity,
    SeaweedSpecFilerAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    SeaweedSpecFilerAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector,
    SeaweedSpecFilerEnv,
    SeaweedSpecFilerEnvValueFrom,
    SeaweedSpecFilerEnvValueFromSecretKeyRef,
    SeaweedSpecFilerLimits,
    SeaweedSpecFilerRequests,
    SeaweedSpecMaster,
    SeaweedSpecMasterAffinity,
    SeaweedSpecMasterAffinityPodAntiAffinity,
    SeaweedSpecMasterAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    SeaweedSpecMasterAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector,
    SeaweedSpecMasterLimits,
    SeaweedSpecMasterRequests,
    SeaweedSpecMasterTolerations,
    SeaweedSpecS3,
    SeaweedSpecS3ConfigSecret,
    SeaweedSpecS3Env,
    SeaweedSpecS3Limits,
    SeaweedSpecS3Requests,
    SeaweedSpecS3Tolerations,
    SeaweedSpecVolumeTopology,
    SeaweedSpecVolumeTopologyAffinity,
    SeaweedSpecVolumeTopologyAffinityPodAntiAffinity,
    SeaweedSpecVolumeTopologyAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    SeaweedSpecVolumeTopologyAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector,
    SeaweedSpecVolumeTopologyEnv,
    SeaweedSpecVolumeTopologyLimits,
    SeaweedSpecVolumeTopologyRequests,
    SeaweedSpecVolumeTopologyTolerations,
)

from cluster.cdk8s import stateful_infra
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import filer_db, namespace, s3_config

NAME = "seaweedfs"
OUTPUT_DIR = "cluster/k8s/seaweedfs/cluster"
_ZONE_SELECTOR = {"topology.kubernetes.io/zone": "hil-ovh"}
_HOSTNAME = "kubernetes.io/hostname"
_CONTROL_PLANE = "node-role.kubernetes.io/control-plane"


def _component_labels(component: str) -> dict[str, str]:
    """The operator's pod labels for one component: the anti-affinity and the PDB select on them."""
    return {"app.kubernetes.io/component": component, "app.kubernetes.io/name": NAME}


def _volume_topology(
    *, rack: str, storage: str, storage_class: str, disk: str, max_volume_counts: int
) -> SeaweedSpecVolumeTopology:
    return SeaweedSpecVolumeTopology(
        replicas=3,
        data_center="hil",
        rack=rack,
        priority_class_name=stateful_infra.NAME,
        # Soft heap ceiling at ~90% of the memory limit below, same pattern as the filer and
        # s3 gateway. Go's GC (GOGC=100) does not know the cgroup limit exists and lets the
        # heap overshoot before the next cycle, so a hard limit with no soft ceiling is a
        # kernel OOM-kill waiting for a busy enough moment -- which is what happened to
        # volume-hdd-0 (OOMKilled 3x, most recently 2026-08-17T22:02:46Z). volume-hdd-1 has
        # since reached 1345Mi against this 1536Mi limit (88%) over 30d. GOMEMLIMIT makes the
        # runtime trade CPU to stay under the cap instead of dying at it.
        env=[SeaweedSpecVolumeTopologyEnv(name="GOMEMLIMIT", value="1400MiB")],
        requests={
            "storage": SeaweedSpecVolumeTopologyRequests.from_string(storage),
            "cpu": SeaweedSpecVolumeTopologyRequests.from_string("100m"),
            "memory": SeaweedSpecVolumeTopologyRequests.from_string("512Mi"),
        },
        limits={"memory": SeaweedSpecVolumeTopologyLimits.from_string("1536Mi")},
        storage_class_name=storage_class,
        # MUST be the same in both groups. Even in topology-only mode the operator also
        # emits a topology-less `seaweedfs-volume-peer` Service (and a `seaweedfs-volume`
        # ServiceMonitor) whose selector matches EVERY volume pod but publishes a single
        # port. With ssd on 9328 that Service scraped the two ssd servers on 9325, which
        # they do not serve: two permanently-failing targets, enough to hold TargetDown
        # above its 10% threshold indefinitely. Nothing else needs these to differ -- 8444
        # and 18444 are already identical across both groups, and separate pods can share a
        # port number freely.
        metrics_port=9325,
        extra_args=[f"-disk={disk}"],
        # Go read-only before the disk fills rather than trusting the slot cap -- the real
        # safety bound once maxVolumeCounts overcommits. On the ssd group NVMe#2 is one
        # shared XFS pool (this ssd server + forgejo-db + filer-db), so this also keeps a
        # reserve for the fragile CNPG DBs.
        min_free_space_percent=10,
        # Overcommit the volume-slot count: -max=0 auto-derives disk_size /
        # volumeSizeLimitMB slots and SeaweedFS pre-grabs ~2-6 (mostly-empty) volumes per
        # collection. Thin volumes make over-committing ~free; disk-full is bounded by
        # minFreeSpacePercent, not the slot cap.
        max_volume_counts=max_volume_counts,
        node_selector={"storage.allegedly.works/tier": disk},
        tolerations=[SeaweedSpecVolumeTopologyTolerations(key=_CONTROL_PLANE, operator="Exists", effect="NoSchedule")],
        affinity=SeaweedSpecVolumeTopologyAffinity(
            pod_anti_affinity=SeaweedSpecVolumeTopologyAffinityPodAntiAffinity(
                required_during_scheduling_ignored_during_execution=[
                    SeaweedSpecVolumeTopologyAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                        label_selector=SeaweedSpecVolumeTopologyAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector(
                            match_labels={"app.kubernetes.io/instance": NAME, "seaweedfs/topology": disk}
                        ),
                        topology_key=_HOSTNAME,
                    )
                ]
            )
        ),
    )


def _filer_db_env(name: str, key: str) -> SeaweedSpecFilerEnv:
    return SeaweedSpecFilerEnv(
        name=name,
        value_from=SeaweedSpecFilerEnvValueFrom(
            secret_key_ref=SeaweedSpecFilerEnvValueFromSecretKeyRef(name=filer_db.CREDENTIALS_SECRET, key=key)
        ),
    )


def seaweed(scope: Construct) -> Seaweed:
    return Seaweed(
        scope,
        "seaweed",
        metadata=metadata(NAME, namespace.NAME),
        spec=SeaweedSpec(
            # 4.x is required for the Bucket CR's access wiring: the filer's gRPC server
            # unconditionally registers `iam_pb.SeaweedIdentityAccessManagement` starting at
            # commit f41925b60 ("Embed IAM API into S3 server", in 4.03). Before that, the
            # operator's BucketAdmin couldn't call the IAM gRPC service (Unimplemented), so
            # every Bucket CR landed Failed regardless of whether the bucket was fresh or
            # pre-existing. The seaweedfs-operator 1.0.19 image imports SeaweedFS at commit
            # a1e5eb9da (between 4.23 and 4.24), so 4.44 remains compatible with the
            # operator's expected wire surface.
            #
            # Upgrade safety: changelog scan from 3.93 to 4.44 found no on-disk format
            # changes (leveldb2 filer store, volume .dat/.idx, s3-config.json all stable), no
            # breaking gRPC changes (only additions to filer_pb / master_pb / iam_pb), and
            # split-brain prevention via persistent ClusterID is single-master-friendly
            # (auto-generates on first start).
            # renovate: datasource=docker
            image="chrislusf/seaweedfs:4.46",
            volume_server_disk_count=1,
            master=SeaweedSpecMaster(
                # 3-instance raft, spread across kimsufi hosts. Quorum 2/3 -> cluster survives
                # any single-node outage. Tolerate the CP taint matching volume + s3.
                replicas=3,
                # SeaweedFS derives each volume server's maximum logical volume count from disk
                # size / volumeSizeLimitMB. 30GB left the first two 1.8TiB servers at 61/61
                # logical volumes after only a handful of collections, so new replicated
                # collections could not grow. 16GB keeps object chunks well below the
                # per-volume cap while leaving enough free slots for new collections.
                volume_size_limit_mb=16000,
                # Accepted posture (operator decision 2026-09-02): single-NODE failure
                # tolerance. 001 = one extra copy on another volume server in the same rack;
                # all three HDD servers carry one rack label, so a rack-level failure at OVH
                # (they are physically in two racks) can take both copies. Rack-fault
                # tolerance would need the HDD group split per physical rack plus repl 010,
                # which strands the singleton H109A09 box with no bulk; whether OVH can supply
                # a node in the other rack is unknown. Not planned; revisit only if a fourth
                # HDD node lands.
                default_replication="001",
                metrics_port=9321,
                # QoS / eviction protection (see cluster/docs/lessons_learned/
                # 2026_06_19_seaweedfs_descheduler_dns_race_crashloop.md). Without requests
                # these pods are BestEffort: the descheduler's first eviction target (QoS
                # tiebreak) and the kernel OOM-killer's first victim under node memory
                # pressure. Requests sized above steady-state (raft metadata is light, ~40Mi
                # observed) so usage normally sits at/below request -> kubelet ranks them last
                # for node-pressure eviction. Memory limit is a generous safety cap; no CPU
                # limit (avoid throttling raft). priorityClassName defers them in the
                # descheduler's primary (priority) sort and shields from preemption.
                priority_class_name=stateful_infra.NAME,
                requests={
                    "cpu": SeaweedSpecMasterRequests.from_string("50m"),
                    "memory": SeaweedSpecMasterRequests.from_string("128Mi"),
                },
                limits={"memory": SeaweedSpecMasterLimits.from_string("512Mi")},
                node_selector=_ZONE_SELECTOR,
                tolerations=[SeaweedSpecMasterTolerations(key=_CONTROL_PLANE, operator="Exists", effect="NoSchedule")],
                # Hard anti-affinity -- each master on a different host so raft quorum can
                # actually survive a node loss.
                affinity=SeaweedSpecMasterAffinity(
                    pod_anti_affinity=SeaweedSpecMasterAffinityPodAntiAffinity(
                        required_during_scheduling_ignored_during_execution=[
                            SeaweedSpecMasterAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                                label_selector=SeaweedSpecMasterAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector(
                                    match_labels=_component_labels("master")
                                ),
                                topology_key=_HOSTNAME,
                            )
                        ]
                    )
                ),
            ),
            # SSD tiering -- cluster/docs/plans/ovh_storage_tiering.md. volumeTopology is the
            # sole volume-server source: the operator reconciles ONLY these groups
            # (all-or-nothing -- it does NOT also honor a flat spec.volume, which we
            # deliberately omit). Each group requires dataCenter + rack and is fully
            # self-contained (no inheritance from a base spec.volume; topology-only deployment
            # needs the operator's nil-safe BaseVolumeSpec fallback, added in 1.0.20).
            volume_topology={
                # 3 servers on the KS-5 HDD nodes; destination for the migrated bulk.
                # -disk=hdd matches the flat servers' default disk type ("" == hdd, verified
                # in seaweedfs 4.29 ToDiskType).
                #
                # The rack label is logical, not physical: OVH racks these boxes across THREE
                # racks (ovh_dedicated_server data source): 102453=H109A09,
                # 103656+103711=H109B04, and the ssd nodes 104952+104963=H108B01. This hdd
                # group carries ONE rack label (matching the flat servers, so volume.move
                # doesn't create transient same-rack placement mismatches). Deliberate: repl
                # 001 (same-rack, 2 nodes) needs >=2 nodes per rack, and a faithful per-rack
                # split would strand the singleton H109A09 (102453, the 3.6 TB disk) with no
                # bulk. Single-node tolerance is the accepted posture -- see
                # defaultReplication above; the split + repl 010 sketch stays in
                # cluster/docs/plans/ovh_storage_tiering.md as an option, not a plan.
                #
                # 400 slots: -max=0 gave only 112/113 on the 1.8 TiB servers, which sat at
                # 112/112 (full) while volumes were ~5% full (~0.85 GB of a 16 GB cap). 400
                # leaves 2*400 = 800 slots across any two survivors; the current 2-copy set
                # uses 652 (326 logical volumes), leaving 148 slots for evacuation and growth.
                # 400 also exceeds the 3.6 TB node's auto-derived 232 slots, so no server is
                # capped below its prior count.
                "hdd": _volume_topology(
                    rack="hil-ovh-h109b04",
                    storage="1800Gi",
                    storage_class="local-path-ovh-hdd",
                    disk="hdd",
                    max_volume_counts=400,
                ),
                # 3 servers on the OVH NVMe nodes; serves Forgejo git via the
                # seaweedfs-ovh-ssd StorageClass (diskType=ssd -> -disk=ssd).
                #
                # Real OVH rack -- both KS-GAME nodes (104952, 104963) are physically in
                # H108B01 (verified via the ovh_dedicated_server data source for the existing
                # nodes). The logical rack label keeps repl 001's two git copies in this group
                # while the SYS-1 provides a third evacuation target.
                #
                # 200 slots: -max=0 auto-derives only ~24 on the 419 GB NVMe. 200 gives
                # 2*200 = 400 slots across any two survivors; the current 2-copy set uses 68
                # (34 logical volumes), leaving room for the planned SSD migrations.
                "ssd": _volume_topology(
                    rack="hil-ovh-h108b01",
                    storage="250Gi",
                    storage_class="local-path-ovh-ssd",
                    disk="ssd",
                    max_volume_counts=200,
                ),
            },
            filer=SeaweedSpecFiler(
                # 2-replica HA. The filer is a stateless metadata gateway to the shared
                # postgres2 store (seaweedfs-filer-db-ssd), so both replicas serve identical
                # data and concurrent writes are handled by enableUpsert. HA lets the filer
                # roll / drain without a SeaweedFS-wide metadata stall, and (with the
                # affinity below) keeps it off control-plane system disks.
                # NOTE: the metadata DB still has no off-cluster WAL/PITR backup -- only the
                # 2-instance CNPG streaming replication. That gap is independent of the filer
                # replica count but worth closing (it is the SSOT for all SeaweedFS data; see
                # the 2026_05_27 filer-metadata-loss lesson).
                replicas=2,
                # Filled in from filer.toml by the directory's kustomization (module docstring).
                config="",
                env=[
                    # Soft heap ceiling ~90% of the memory limit below; see the sizing note at
                    # requests/limits. Go's GC (GOGC=100) otherwise ignores the cgroup limit
                    # and overshoots -> kernel OOM-kill.
                    SeaweedSpecFilerEnv(name="GOMEMLIMIT", value="700MiB"),
                    # Postgres credentials override filer.toml [postgres2] keys via viper's
                    # env-var binding (WEED_<key>, dot->underscore); CNPG syncs the Secret onto
                    # the DB's seaweedfs role.
                    _filer_db_env("WEED_POSTGRES2_USERNAME", "username"),
                    _filer_db_env("WEED_POSTGRES2_PASSWORD", "password"),
                ],
                metrics_port=9326,
                # QoS / eviction protection as on master. With 2 replicas, a minAvailable:1
                # PDB keeps the descheduler / node drains from taking both filers down at
                # once.
                #
                # Memory sizing: the filer is a metadata gateway to Postgres (postgres2
                # backend -- metadata lives in the DB, not in-pod), so steady-state is modest
                # (~190Mi peak / ~136Mi current in our Mimir history). BUT the filer idle floor
                # jumped in SeaweedFS 4.18+: per upstream
                # https://github.com/seaweedfs/seaweedfs/issues/9035 the filer now uses
                # 250-360Mi idle (vs 80-90Mi in 4.17), and that issue's reporter OOM-loops a
                # 256Mi-limited filer every 3-5min. Sized on data, same pattern as the s3
                # gateway below:
                #   - request 384Mi: above the 360Mi reported idle ceiling, so kubelet
                #     node-pressure eviction ranks it last.
                #   - limit 768Mi: ~2x the reported idle ceiling, ~4x our observed peak.
                #   - GOMEMLIMIT=700MiB (set in env above): soft ceiling so Go GCs harder
                #     instead of being OOM-killed at the cap.
                priority_class_name=stateful_infra.NAME,
                requests={
                    "cpu": SeaweedSpecFilerRequests.from_string("50m"),
                    "memory": SeaweedSpecFilerRequests.from_string("384Mi"),
                },
                limits={"memory": SeaweedSpecFilerLimits.from_string("768Mi")},
                node_selector=_ZONE_SELECTOR,
                affinity=SeaweedSpecFilerAffinity(
                    # Hard anti-affinity -- one filer per host, so the 2 replicas never share a
                    # node and a single node loss can't take out both.
                    pod_anti_affinity=SeaweedSpecFilerAffinityPodAntiAffinity(
                        required_during_scheduling_ignored_during_execution=[
                            SeaweedSpecFilerAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                                label_selector=SeaweedSpecFilerAffinityPodAntiAffinityRequiredDuringSchedulingIgnoredDuringExecutionLabelSelector(
                                    match_labels=_component_labels("filer")
                                ),
                                topology_key=_HOSTNAME,
                            )
                        ]
                    ),
                    # Prefer ordinary workers; this soft rule only affects filer CP fallback.
                    node_affinity=SeaweedSpecFilerAffinityNodeAffinity(
                        preferred_during_scheduling_ignored_during_execution=[
                            SeaweedSpecFilerAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecution(
                                weight=100,
                                preference=SeaweedSpecFilerAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreference(
                                    match_expressions=[
                                        SeaweedSpecFilerAffinityNodeAffinityPreferredDuringSchedulingIgnoredDuringExecutionPreferenceMatchExpressions(
                                            key=_CONTROL_PLANE, operator="DoesNotExist"
                                        )
                                    ]
                                ),
                            )
                        ]
                    ),
                ),
            ),
            s3=SeaweedSpecS3(
                replicas=2,  # stateless S3 gateway -- scales freely across the 3 kimsufi hosts
                config_secret=SeaweedSpecS3ConfigSecret(name=s3_config.SECRET_NAME, key=s3_config.SECRET_KEY),
                # `weed s3` only reads seaweedfs-s3-config at startup, so it must roll when ESO
                # reassembles that Secret (tenant added/rotated). NOTE: this annotation lands
                # on the *pod template*, not the Deployment's own metadata, and Reloader only
                # reads the workload-level annotation -- so it is INERT here. The actual roll
                # comes from Reloader's cluster-wide `autoReloadAll: true` (see
                # cluster/k8s/reloader/). Kept for intent/future-proofing if the operator ever
                # sets Deployment annotations.
                annotations={"reloader.stakater.com/auto": "true"},
                metrics_port=9327,
                # QoS / eviction protection. No PDB: it is stateless and freely
                # reschedulable, so descheduler moves are harmless.
                #
                # Memory is load-driven, NOT a fixed footprint: it tracks concurrent in-flight
                # transfers (our load is dominated by Mimir TSDB block range-GETs -- verified
                # via SeaweedFS_s3_request_total) plus the in-process Iceberg REST catalog. The
                # in-memory chunk cache (-cacheCapacityMB) is off (default 0), so there is
                # nothing to cap there. The upstream Helm chart's 128Mi/512Mi is sized for a
                # *lightweight* gateway; ours is not. Mimir query/compaction bursts drove the
                # pre-limit pods to ~1.7Gi peak (observed in our own Mimir history), so a 512Mi
                # limit hard-OOM-looped the pod every ~10min (exit 137). Sizing, on data:
                #   - request 512Mi: above steady-state (~400Mi) so kubelet node-pressure
                #     eviction ranks it last (same logic as the volume servers above).
                #   - limit 2Gi: comfortably above the observed ~1.7Gi burst peak.
                #   - GOMEMLIMIT=1800MiB: soft ceiling ~90% of the limit. Go's GC otherwise
                #     ignores the cgroup limit (GOGC=100 lets the heap overshoot before the
                #     next GC -> kernel OOM-kill). This makes the runtime trade CPU to stay
                #     under the cap instead of dying -- the "avoid container-OOM-on-own-limit"
                #     caveat from the 2026_06_19 descheduler RCA, done properly for s3.
                priority_class_name=stateful_infra.NAME,
                env=[SeaweedSpecS3Env(name="GOMEMLIMIT", value="1800MiB")],
                requests={
                    "cpu": SeaweedSpecS3Requests.from_string("50m"),
                    "memory": SeaweedSpecS3Requests.from_string("512Mi"),
                },
                limits={"memory": SeaweedSpecS3Limits.from_string("2Gi")},
                # weed s3 defaults -ip.bind to localhost -- kubelet's readiness probe from
                # outside the pod gets "connection refused". Bind to 0.0.0.0 so probes (and
                # the Service ClusterIP) reach the API.
                extra_args=["-ip.bind=0.0.0.0"],
                node_selector=_ZONE_SELECTOR,
                # Allow kimsufi CPs as scheduling targets so s3 can spread across OVH hosts
                # when desirable. Stateless gateway -- no I/O contention.
                tolerations=[SeaweedSpecS3Tolerations(key=_CONTROL_PLANE, operator="Exists", effect="NoSchedule")],
            ),
        ),
    )


def _pod_disruption_budget(scope: Construct, component: str, *, min_available: int) -> None:
    k8s.KubePodDisruptionBudget(
        scope,
        f"pdb-{component}",
        metadata=k8s.ObjectMeta(name=f"{NAME}-{component}", namespace=namespace.NAME),
        spec=k8s.PodDisruptionBudgetSpec(
            min_available=k8s.IntOrString.from_number(min_available),
            selector=k8s.LabelSelector(match_labels=_component_labels(component)),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    seaweed(chart)
    AdminScript(
        chart,
        "replication-repair",
        metadata=metadata(
            "replication-repair",
            namespace.NAME,
            annotations={"description": "Hourly copy-only repair of SeaweedFS volume replica placement."},
        ),
        spec=AdminScriptSpec(
            cluster_ref=AdminScriptSpecClusterRef(name=NAME),
            schedule="17 * * * *",
            time_zone="Etc/UTC",
            concurrency_policy=AdminScriptSpecConcurrencyPolicy.FORBID,
            restart_policy=AdminScriptSpecRestartPolicy.NEVER,
            backoff_limit=0,
            starting_deadline_seconds=300,
            active_deadline_seconds=21600,
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=3,
            resources=AdminScriptSpecResources(
                requests={
                    "cpu": AdminScriptSpecResourcesRequests.from_string("100m"),
                    "memory": AdminScriptSpecResourcesRequests.from_string("128Mi"),
                },
                limits={"memory": AdminScriptSpecResourcesLimits.from_string("512Mi")},
            ),
            # Keep the boolean value attached: `-doDelete false` parses as true. The CLI
            # otherwise also deletes excess/misplaced replicas. Pruning after a node returns
            # remains a deliberate operator action.
            script=(
                "lock\n"
                "volume.fix.replication -apply -doDelete=false -maxParallelization=2"
                " -maxParallelizationPerServer=1 -retry=5\n"
                "unlock\n"
            ),
        ),
    )
    # The descheduler honors PDBs unconditionally, so these are the lever that stops it (and
    # `kubectl drain` / node-autoscaler) from breaking quorum via voluntary eviction -- the
    # trigger behind the 2026-06-19 crash-loop incident (cluster/docs/lessons_learned/
    # 2026_06_19_seaweedfs_descheduler_dns_race_crashloop.md). minAvailable 2 of 3 on
    # master/volume preserves raft quorum and >=2 replicas (replication 001); 1 of the 2
    # filers keeps a drain from causing a SeaweedFS-wide metadata stall. The stateless s3
    # gateway deliberately gets no PDB.
    _pod_disruption_budget(chart, "master", min_available=2)
    _pod_disruption_budget(chart, "volume", min_available=2)
    _pod_disruption_budget(chart, "filer", min_available=1)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
