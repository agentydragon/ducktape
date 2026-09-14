# KubeVirt VM Image Artifacts

KubeVirt bootstrap disks are published as S3 objects in SeaweedFS, then imported
into persistent VM root PVCs with CDI `DataVolume` resources.

## Public Endpoint

Use the consolidated public S3 endpoint:

```bash
https://s3.allegedly.works
```

This route is backed by the `seaweedfs/public-s3` Service. Do not route it to
the operator-managed `seaweedfs-s3` Service, which mounts the all-tenant S3
config (admin + every write key).

The `public-s3` gateway mounts a curated multi-identity config; its vm-images
identities are:

- `vm-images-ci-writer`: read/write/list/tagging on bucket `vm-images`
- `vm-images-cdi-reader`: read/list on bucket `vm-images`

## Source Of Truth

The `vm-images` Bucket and its SeaweedFS identities are managed by the single
`vm-images-publisher` Flux Kustomization. Its `S3Credentials` resources generate
the publisher-local `vm-images-ci-writer-s3-credentials` and
`vm-images-cdi-reader-s3-credentials` Secrets. Do not manually create or copy
these Secrets during normal operation; Flux and the SeaweedFS operator own them.

The writer Job reads its local Secret directly. CDI reader Secrets in consumer
namespaces are projected by the app-local ExternalSecrets in agent-box and gecko.

## Publishing A Bootstrap Image

`cluster/k8s/vm-images-publisher/` carries a suspended CronJob that runs
`nix build .#bootstrap-image` and uploads the qcow2 to SeaweedFS through the
internal S3 endpoint. Operators trigger a publish with:

```bash
kubectl create job --from=cronjob/vm-images-publisher \
  "publish-$(date +%s)" -n vm-images-publisher
```

See <../k8s/vm-images-publisher/README.md> for the runbook. Object keys are
commit-addressed (`bootstrap/<sha>.qcow2`); existing VMs do not auto-replace
their root PVC when a new image is published.

## Importing With CDI

Create a reader Secret in the VM namespace using the CDI key names:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: gecko-vm-images-s3-reader
  namespace: gecko
type: Opaque
stringData:
  accessKeyId: <vm-images-cdi-reader access key>
  secretKey: <vm-images-cdi-reader secret key>
```

Then import the qcow2 into a root PVC:

```yaml
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataVolume
metadata:
  name: gecko-root
  namespace: gecko
spec:
  source:
    s3:
      url: "https://s3.allegedly.works/vm-images/bootstrap/<commit>.qcow2"
      secretRef: gecko-vm-images-s3-reader
  pvc:
    accessModes:
      - ReadWriteOnce
    resources:
      requests:
        storage: 20Gi
```

After import, boot the VM from `gecko-root`, SSH in, and switch to the real host
config:

```bash
sudo nixos-rebuild switch --flake github:agentydragon/ducktape?ref=devel#gecko
```

## Paving Notes

- The `vm-images` Bucket must exist before the public gateway starts. SeaweedFS
  can auto-create bucket directories from identity actions, which bypasses the
  Bucket CR adoption path. The Flux wiring applies `vm-images-publisher` first
  and gates `seaweedfs-public-s3` on it.
- New SeaweedFS collections need free logical volume slots on enough volume
  servers to satisfy `defaultReplication: "001"`. The bootstrap publish path
  exposed this when the old 30GB `volumeSizeLimitMB` left two volume servers at
  their computed 61/61 slot limit before `vm-images` had any writable volumes.
  Keep the lower 16GB limit unless the volume-server capacity model changes.
- The public `s3.allegedly.works` HTTPRoute is for **reads only** for the
  vm-images bucket (CDI imports). Writes from GitHub-hosted runners over this
  path sustained ~250 KiB/s and could not complete multi-GiB uploads inside
  Envoy's stream timeout window — the publisher therefore runs in-cluster
  against `http://public-s3.seaweedfs.svc:8333`.
- The first manual spike created an aggregate vm-images Secret directly and was
  removed. The paved path is SeaweedFS `S3Credentials` -> operator-generated
  local Secrets -> app-local ExternalSecret projections where needed.
- The dedicated gateway runs as non-root and uses the restricted PodSecurity
  settings expected by current namespace admission.
