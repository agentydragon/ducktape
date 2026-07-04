# SeaweedFS `volumeTopology` migration + operator upgrade (2026-07-04)

Lessons from Stage 1 of the OVH storage tiering (<../plans/ovh_storage_tiering.md>) —
splitting the flat SeaweedFS volume layer into `hdd`/`ssd` `volumeTopology` groups and
upgrading the operator. The stale-mount-cache incident that the evacuation caused has its
own writeup: <2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md>.

## `spec.volumeTopology` is all-or-nothing, not additive

The seaweedfs-operator reconciles `spec.volume` **or** `spec.volumeTopology`, never both:
`controller_volume.go` does `if len(VolumeTopology) > 0 { return ...topology }` (confirmed
in 1.0.19 and master). The moment `volumeTopology` is set, the flat `spec.volume`
StatefulSet is **no longer reconciled** — an operator upgrade won't change this. So you
can't "add an SSD group beside" the existing flat servers; the whole volume layer has to
move to two topology groups, and the existing data is `volume.move`-migrated onto the new
group. Each group requires `dataCenter` + `rack` (CRD-required), and must be **fully
self-contained** (own replicas/resources/priorityClass/storageClass/metricsPort/tolerations/
affinity) — nothing inherits from `spec.volume`.

## `spec.volume` stub nil-panics ≤1.0.19; fixed in 1.0.20

On operator **1.0.19**, removing `spec.volume` while `volumeTopology` is set nil-panics
`buildVolumeServerStartupScriptWithTopology` (it dereferences `m.Spec.Volume.*` for topology
defaults). So the stub had to stay as a defaults placeholder that creates no servers.
**1.0.20** shipped "nil-safe spec.volume fallback in topology startup script" + "nil-safe
BaseVolumeSpec for topology-only deployments" — after upgrading (we went to 1.0.30) the stub
was dropped cleanly (a no-op in topology mode: no data-plane churn, `spec.volume` gone).

## Operator upgrade re-renders and rolls the **entire** data plane

Bumping the operator (1.0.19 → 1.0.30) re-rendered the master/volume/filer StatefulSets —
the newer operator adds a `sw-security` volume mount (`/etc/sw-security`) to the pod spec —
so **all** of them did a one-time rolling restart. It's safe (StatefulSet `RollingUpdate`,
one pod at a time, pod identity preserved so no stale-mount risk, data on the PVCs), but
**expect a full data-plane roll on any operator bump** and gate it on **G-swfs** (0
under-replicated) throughout. The data image is pinned in the CR (`spec.image:
chrislusf/seaweedfs:4.29`), so the upgrade touches only the controller + the rendered specs,
not the SeaweedFS version.

## Native volume-server evacuation exists in ≥1.0.28

1.0.28 added a `volume_evacuation.go` controller (drain-before-scale-down). On the current
1.0.30, retiring a volume server is "lower the group's `replicas`, the operator drains first" —
so the ad-hoc `volume.move` evacuation script we built for the one-off Phase 2 flat-server
retirement was removed once we were on 1.0.30. (The stale-mount ordering rule still governs the
final **deletion** — refresh clients while the emptied server is still alive.)
