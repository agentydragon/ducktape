# GitLab + Gitaly storage bench

Companion to <../distributed_storage_and_tiny_rook_ceph.md>. That trial showed the
Forgejo git-write penalty is a **shared-RWX** problem: multiple Forgejo replicas mount
one git PVC `RWX`, and that shared mount tanks git push (badly on SeaweedFS, and even on
CephFS via MDS cap coordination). GitLab's architecture avoids the shared mount — stateless
webservice replicas proxy to a single **Gitaly** that owns the repos on one `RWO` PVC. This
harness benches whether that single-writer design keeps git push fast on network-backed
storage across `{rook, seaweedfs} x {hdd, ssd}`.

Same four operations and sample counts as the Forgejo bench (`../forgejo_git_write_latency.md`):
20x API `version`, 20x contents read, 20x contents write, 20 tiny git pushes over HTTP.

## Design

- 3 webservice replicas (stateless) + 1 Gitaly (single writer). **Only Gitaly's storage
  class varies per cell**; PG/Redis/MinIO stay on `local-path-ovh-ssd` so the comparison
  isolates the git repo store (same discipline as the Forgejo bench holding the DB on SSD).
- Deployed via `helm` (throwaway bench, not Flux) — GitLab chart `9.11.8` (last 9.x with
  bundled PG/Redis/MinIO; v10 requires external PostgreSQL).
- The bench Job hits the webservice ClusterIP in-cluster (no ingress), like the Forgejo bench.

## Files

- `values-cell.yaml.tmpl` — helm values; `__GITALY_SC__` is the per-cell storage class.
- `gitlab-bench.sh` — the bench script (GitLab API v4, `PRIVATE-TOKEN` + `oauth2:` git auth).
- `bench-job.yaml.tmpl` — bench Job; `__TARGET_NAME__` is the cell label.

## Run one cell

```bash
CELL=seaweedfs-ovh-ssd          # storage class for Gitaly
LABEL=seaweedfs-ssd             # short label used in output rows

# 1. Deploy / reconfigure GitLab with Gitaly on this cell.
sed "s/__GITALY_SC__/$CELL/" values-cell.yaml.tmpl > /tmp/values.yaml
helm upgrade --install gitlab-bench gitlab/gitlab -n gitlab-bench --create-namespace \
  --version 9.11.8 -f /tmp/values.yaml --timeout 25m --wait=false
kubectl label ns gitlab-bench pod-security.kubernetes.io/enforce=privileged --overwrite

# 2. Wait for convergence.
kubectl -n gitlab-bench wait --for=condition=complete job -l app=migrations --timeout=1200s
kubectl -n gitlab-bench rollout status deploy -l app=webservice --timeout=600s

# 3. Mint a root PAT and stash it as a Secret.
TOKEN=$(kubectl -n gitlab-bench exec deploy/gitlab-bench-toolbox -c toolbox -- \
  gitlab-rails runner \
  "puts User.find_by_username('root').personal_access_tokens.create!(scopes:['api','write_repository'],name:'bench',expires_at:1.day.from_now).token" \
  2>/dev/null | tail -1)
kubectl -n gitlab-bench create secret generic gitlab-bench-token \
  --from-literal=token="$TOKEN" --dry-run=client -o yaml | kubectl apply -f -

# 4. Load the script + run the Job.
kubectl -n gitlab-bench create configmap gitlab-bench-script \
  --from-file=gitlab-bench.sh --dry-run=client -o yaml | kubectl apply -f -
sed "s/__TARGET_NAME__/$LABEL/" bench-job.yaml.tmpl | kubectl apply -f -

# 5. Collect results (CSV rows: target,operation,http_status,seconds).
kubectl -n gitlab-bench logs -l app.kubernetes.io/name=gitlab-storage-bench --tail=200 \
  | grep -E "^$LABEL,"
```

Repeat steps 1–5 per cell. `rook-ssd` reuses the live trial Ceph cluster
(`rook-cephfs-trial-ssd`). `rook-hdd` needs a **sequential rebuild** of the trial Ceph
cluster onto the HDD hosts, because a single CephCluster can't mix `dataDirHostPath` roots
per node (see <../distributed_storage_and_tiny_rook_ceph.md>).

### Rook HDD arm rebuild

Manifest: <rook-hdd-arm.yaml> (3 HDD hosts `ovh-ns{102453,103656,103711}`, 3-copy,
loop-backed OSDs on `/var/mnt/local-path-ovh-hdd`, StorageClass `rook-cephfs-trial-hdd`).

```bash
# 1. Tear down the SSD arm. Rook's finalizer chain is
#    SubVolumeGroup -> CephFilesystem -> CephCluster, and the SubVolumeGroup won't delete
#    while CSI subvolumes (bench PVCs) linger. For a DISPOSABLE trial, force it:
kubectl -n rook-ceph delete cephfilesystem trialfs-ssd --wait=false
kubectl delete sc rook-cephfs-trial-ssd
for cr in cephfilesystemsubvolumegroup/trialfs-ssd-csi cephfilesystem/trialfs-ssd \
          cephblockpool/builtin-mgr cephcluster/rook-ceph; do
  kubectl -n rook-ceph patch ${cr%/*} ${cr#*/} --type merge -p '{"metadata":{"finalizers":[]}}'
done
# force-clearing the CephCluster finalizer orphans its daemon pods — delete them:
kubectl -n rook-ceph delete deploy -l app=rook-ceph-mon
kubectl -n rook-ceph delete deploy -l app=rook-ceph-osd
kubectl -n rook-ceph delete deploy -l app=rook-ceph-mgr
kubectl -n rook-ceph delete deploy -l app=rook-ceph-mds
kubectl -n rook-ceph delete daemonset rook-ceph-trial-loop-device-ssd

# 2. Deploy the loop-device DaemonSet FIRST and confirm every node landed on /dev/loop3
#    (the CephCluster selects it literally; losetup picking a different loop breaks OSDs):
awk '/^---/{exit} {print}' rook-hdd-arm.yaml | kubectl apply -f -
kubectl -n rook-ceph logs -l app.kubernetes.io/name=rook-ceph-trial-loop-device-hdd | grep ready:

# 3. Apply the rest (CephCluster + CephFilesystem + StorageClass), wait for health:
kubectl apply -f rook-hdd-arm.yaml
# watch: kubectl -n rook-ceph get cephcluster,cephfilesystem  (osds 3/3, fs Ready)

# 4. Point Gitaly at rook-cephfs-trial-hdd (step 1 reconfigure with __GITALY_SC__) and bench.
```

## Teardown

```bash
helm uninstall gitlab-bench -n gitlab-bench
kubectl delete ns gitlab-bench
# rook trial: delete CephFilesystem, then CephCluster (finalizer chain as above);
# then the loop-device DaemonSet, and finally the loop backing files + detach the loop
# devices on the OVH nodes (host access) and remove the CephCluster dataDirHostPath.
```
