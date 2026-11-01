# KubeVirt VM Image Artifacts

Current KubeVirt VM images are published as OCI `containerDisk` images in the
Forgejo registry. The image contains a qcow2 disk; KubeVirt pulls the OCI image
and uses the disk as the VM's ephemeral root. This is the pattern used by the
CPAP gateway and Agentplane runner images:

- Build recipes: <../../cpap/gateway/container-disk.nix> and
  <../../agentplane/env/images/container-disk.nix>.
- Publishing workflows: <../../.github/workflows/cpap-gateway-image.yml> and
  <../../.github/workflows/agentplane-runner-vm-image.yml>.
- KubeVirt definitions: <../cdk8s/cpap_sync/app.py> and the Agentplane staging
  SandboxTemplate in <../cdk8s/agentplane/staging.py>.

The Agentplane VM attaches two: the guest root and a small read-only erofs disk with the
Environment Runtime, Harness Host, Execution service and `agentplane` command
(`//agentplane/env/images:disk_image`, built by Bazel and published
by the `push-images` workflow from <../../devinfra/ci/image_targets.json>), so a runner release does
not republish the guest.

Use `containerDisk` for disposable roots. Put state that must survive VM restarts
on a PVC or another durable store. Updating an OCI tag does not replace the disk
of an already running VMI by itself; use the VM's rollout/restart path and verify
the new image after it starts.

The legacy `vm-images` SeaweedFS bucket, its S3 identities, and the suspended
qcow2 publisher CronJob have been retired. The parked `gecko` and `agent-box`
manifests still describe CDI imports from that bucket; they are archival and
cannot be unparked as written. Port them to OCI `containerDisk` images first.
