# KubeVirt VM Image Artifacts

KubeVirt bootstrap disks are published as S3 objects in SeaweedFS, then imported
into persistent VM root PVCs with CDI `DataVolume` resources.

## Public Endpoint

Use the dedicated endpoint:

```bash
https://vm-images-s3.allegedly.works
```

This route must point only at the `seaweedfs/vm-images-s3` Service. Do not route
it to the operator-managed `seaweedfs-s3` Service, which mounts the all-tenant S3
config.

The dedicated gateway mounts a separate S3 config with only:

- `vm-images-ci-writer`: read/write/list/tagging on bucket `vm-images`
- `vm-images-cdi-reader`: read/list on bucket `vm-images`

## Source Of Truth

Secrets come from SOPS and are applied by Flux:

```text
cluster/k8s/seaweedfs/vm-images-s3/credentials.sops.yaml
```

Do not manually create `vm-images-s3-credentials` during normal operation. Local
admin shells may not be able to decrypt this file because it is encrypted to the
cluster SOPS key; that is intentional. Commit and push the SOPS file, then let
Flux decrypt it.

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
      url: "https://vm-images-s3.allegedly.works/vm-images/bootstrap/<commit>.qcow2"
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
  Bucket CR adoption path. The Flux wiring applies `seaweedfs-vm-images-bucket`
  first and gates `seaweedfs-vm-images-s3` on it.
- New SeaweedFS collections need free logical volume slots on enough volume
  servers to satisfy `defaultReplication: "001"`. The bootstrap publish path
  exposed this when the old 30GB `volumeSizeLimitMB` left two volume servers at
  their computed 61/61 slot limit before `vm-images` had any writable volumes.
  Keep the lower 16GB limit unless the volume-server capacity model changes.
- The public `vm-images-s3.allegedly.works` HTTPRoute is for **reads only**
  (CDI imports). Writes from GitHub-hosted runners over this path sustained
  ~250 KiB/s and could not complete multi-GiB uploads inside Envoy's stream
  timeout window — the publisher therefore runs in-cluster against
  `http://vm-images-s3.seaweedfs.svc:8333`.
- The first manual spike created `vm-images-s3-credentials` directly and was
  removed. The paved path is SOPS -> Flux -> Kubernetes Secret -> ExternalSecret
  rendered gateway config.
- The dedicated gateway runs as non-root and uses the restricted PodSecurity
  settings expected by current namespace admission.
