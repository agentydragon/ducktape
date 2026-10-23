# Piraeus disk accounting

## Investigation — 2026-10-23

Live Mimir had 32 `piraeus-ssd-local` claims and kubelet usage for all 32,
but none joined `disk_usage:pvc_filesystem:info`. That rule intentionally uses
host/local PV paths, which CSI PVs do not have. LINSTOR and DRBD metrics were
not being scraped; the custom LVM collector reported percentages only.

A read-only request to the deployed LINSTOR v1.35.2 controller confirmed native
volume labels `resource`, `node`, `volume`, and `storage_pool`. The resource
matches kube-state-metrics' `csi_volume_handle`; the node is the storage host,
not the controller or consumer host. Native pool labels include `driver` and
`backing_pool`, currently `LVM_THIN` and `piraeus_ssd/data`.

| Storage node  | Native pool capacity (bytes) | Native pool used (bytes) | Sum of volume allocations (bytes) |
| ------------- | ---------------------------: | -----------------------: | --------------------------------: |
| ovh-ns1001419 |                 503517806592 |              44359921664 |                       44370209792 |
| ovh-ns104952  |                 441505021952 |              42252029952 |                       42275079168 |
| ovh-ns104963  |                 441505021952 |              21898649600 |                       21908557824 |

These are dated samples, not current capacity gates. LVM pool sizes matched
native capacity, and native used/capacity matched rounded LVM data percentages.
The allocation sums differed from pool use by 9.5–23 MiB; treat per-volume
allocation as an estimate rather than forcing the two to reconcile.

## Accounting contract

- Host ext4/XFS filesystems retain path-based attribution. DRBD filesystems are
  excluded from host capacity totals. Kubelet measurement is a fallback for a
  PVC without application detail, so application and filesystem bytes are not
  added for the same claim.
- Thin-pool capacity/free/used come from LINSTOR's `LVM_THIN` storage pools.
  The node/pool pair identifies each configured pool; do not configure multiple
  LINSTOR pool aliases over one backing thin pool and sum their capacities.
- Volume allocation and capacity belong to each diskful storage-node copy.
  Copies without a Pod or current PV remain visible by resource identity.
  Retained PV claim references preserve their PV phase and resource identity;
  a reused claim name alone does not identify an old retained copy.
- Commitments sum LINSTOR volume-definition limits for diskful copies, including
  block volumes. They are separate from actual allocation and may exceed pool
  capacity under the accepted overcommit policy. No alert pages solely on that
  ratio; existing LVM data/metadata alerts remain, with a physical-free-space
  forecast warning after sufficient history.
- Kubelet filesystem use belongs to the consumer node. Raw block and unmounted
  volumes lack that measurement; absence is not zero allocation.
- Machine selection applies to node-owned panels. S3 bucket totals lack storage
  host ownership and remain explicitly cluster-wide.

## Recording-rule pipeline

The `piraeus-storage` group keeps these dependent rules together and in evaluation
order. Pool totals establish which node/pool pairs are diskful. Native volume
definition sizes normalize LINSTOR's `name` and `volume_number` labels to
`resource` and `volume`; LINSTOR volume state is then restricted to those
diskful pools. That per-copy inventory is collapsed to resource identity and
joined with kube-state-metrics PV/claim/phase metadata when it exists. The
allocated-size rule sums native allocation per diskful resource copy, while the
capacity rule sums normalized definition sizes per copy. Both final rules add
the same optional PV metadata, so resources without a current PV remain visible.

Each multi-stage query is a complete `.promql` expression. `rules.py` registers
the expression file and its recording-rule name; it does not assemble query
fragments. This keeps the label normalization, joins, and output labels reviewable
as the exact expressions Prometheus evaluates.

## Verification boundary

The metadata join resolved all 32 live Piraeus PVs. Read-only Mimir probes of all
48 dashboard targets parsed successfully for All, `ovh-ns104963`, and
`ovh-ns103711`. Existing collector targets returned data where applicable;
22 Piraeus targets await deployment of the native scrape and recording rules.
The 22 expressions in the generated disk-usage PrometheusRule parsed successfully
through Mimir after the pipeline refactor. Newly introduced recording stages do
not have live series until deployment, so this confirms query acceptance rather
than runtime samples. Neither probe establishes browser interpolation or
rendering. Full Grafana verification remains required after the metrics are
available.
