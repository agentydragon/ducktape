# CRI image cleanup while imports settle

The [wyrm2 KVM run](https://app.buildbuddy.io/invocation/00063d13-0b8b-4e77-a19c-db43d601104a)
failed in stack setup on 2026-11-04 UTC. Its source was `3b6893a722` plus the
cert-manager readiness repair `0169f616f5`; it did not reach the VM scenarios.

`prune_unused_images` selected config ID
`sha256:b9a741948e5198e72a28a2d05aa353f363f71aae2574a8d805cfcf8d89ab7d93`
as untagged, unpinned and unused. The retained `containerd.log` shows:

- 03:58:48.969: create `quay.io/jetstack/trust-manager:v0.25.0`, labeled pinned.
- 03:58:49.788: `RemoveImage` receives the config ID.
- 03:58:49.847: CRI creates the config-ID alias and updates the named image's
  managed/pinned labels.
- 03:58:50.048: delete the config-ID alias.
- 03:58:50.595: `RemoveImage` returns successfully.

The final `storage/final/cri-images.json` still lists that config ID, now with the
trust-manager tag and `pinned: true`. The cleanup postcondition checked only ID
presence, so it rejected the current pinned image after its dangling alias was removed.
An empty `image-prune.log` output was not evidence of failed removal: `crictl` output
was captured from stdout only, and containerd recorded success.

The repair applies the same eligibility predicate before and after removal. A surviving
ID is an error only while it remains untagged, undigested, unpinned and unused. Tests
cover references becoming visible during removal and preserve the failing case for a
surviving obsolete image. This does not establish full KVM scenario acceptance.
