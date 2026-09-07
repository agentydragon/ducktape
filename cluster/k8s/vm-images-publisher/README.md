# vm-images-publisher

In-cluster builder + publisher for the NixOS qcow2 image outputs used by CDI to
provision KubeVirt VMs (for example `.#bootstrap-image` and
`.#agent-box-image`). A VM whose root disk doesn't need to persist across
image updates is usually better served by a KubeVirt `containerDisk` published
via GitHub Actions + Flux image automation instead — see
`.github/workflows/cpap-gateway-image.yml` and
`.github/workflows/public-coder-devbox-image.yml` for that pattern.

Why this exists: a GitHub Actions workflow previously did the publish from
runner side and uploaded through the public S3 gateway at
`s3.allegedly.works`. Sustained throughput from GitHub-hosted runners
over that path was ~250 KiB/s — too slow for a multi-GiB qcow2 to complete
inside Envoy's stream-timeout window. This in-cluster Job uses the internal
SeaweedFS S3 endpoint (`http://public-s3.seaweedfs.svc.cluster.local:8333`)
instead, which has no such constraint. The legacy workflow has been removed.

## Manifests

- `namespace.yaml` — privileged PodSecurity (Job needs `/dev/kvm` for the
  qemu-efi image builder).
- `s3-credentials.yaml` — cross-namespace ExternalSecret pulling the
  `ciWriterAccessKey` / `ciWriterSecretKey` from
  `seaweedfs/vm-images-s3-credentials` and rendering them as
  `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars.
- `cronjob.yaml` — suspended CronJob carrying the PodTemplate. Operators
  trigger one-off Jobs from it via `kubectl create job --from=cronjob/…`.
- `publish.sh` — the build + upload script, mounted into the pod via
  `configMapGenerator`.

## Running a publish

```bash
# Default: build current devel HEAD and publish bootstrap/<sha>.qcow2
kubectl create job --from=cronjob/vm-images-publisher \
  "publish-$(date +%s)" -n vm-images-publisher

# Publish the full agent-box image instead
job_name="publish-agent-box-$(date +%s)"
kubectl create job --from=cronjob/vm-images-publisher "$job_name" \
  -n vm-images-publisher
kubectl -n vm-images-publisher set env "job/$job_name" \
  IMAGE_OUTPUT=agent-box-image OBJECT_PREFIX=agent-box

# Watch
kubectl -n vm-images-publisher logs -f -l batch.kubernetes.io/job-name=publish-…
```

To publish from a non-default ref, patch `REF` (or `FLAKE_BASE` /
`IMAGE_OUTPUT`) in the Job template after `kubectl create job --from=cronjob/…`:

```bash
kubectl -n vm-images-publisher set env job/publish-… REF=my-feature-branch
```

…or edit the resulting Job manifest before it starts a pod.

The script resolves `REF` to a commit SHA via `git ls-remote`, then uploads
`bootstrap/<sha>.qcow2` plus a `bootstrap/<sha>.qcow2.sha256` sidecar and a
`bootstrap/<ref>.latest.txt` pointer.

## Verifying

```bash
# From inside the cluster (or via port-forward):
kubectl -n seaweedfs exec deploy/public-s3 -- \
  wget -qO- "http://seaweedfs-filer:8888/buckets/vm-images/bootstrap/" | \
  grep '\.qcow2"'
```

CDI `DataVolume` resources reference the public `s3.allegedly.works`
endpoint (reads work fine on that path; only writes were the problem).
