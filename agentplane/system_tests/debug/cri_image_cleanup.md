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

## A later import was actually deleted

After the worker's seccomp changes, the
[full KVM revalidation](https://app.buildbuddy.io/invocation/286e880e-d146-41ee-b804-c8019df85345)
at devel `040c86945b996d2187a96744b16bc9fa4b5bf8e4` passed stack setup and
started all three VM scenarios. They could not boot because cleanup deleted
`git.allegedly.works/ducktape-ci/agentplane-vm-guest-root:unset` before use.
The retained containerd log records this sequence on November 4 UTC:

- 06:06:35.972: create the guest-root reference, labeled pinned.
- 06:06:36.702: create config-ID alias
  `sha256:52c70eea6cbdb91f5e33a35dfdc35618939db66ab4b020e7c6000e24fca70422`
  and update the named reference's managed/pinned labels.
- 06:06:37.008: `RemoveImage` receives that config ID.
- 06:06:37.168: delete the named guest-root reference.
- 06:06:37.246: delete its config-ID alias.
- 06:06:38.029: removal returns successfully.

`image-prune.log` lists the same ID and reports deletion of the guest-root tag.
Both the final containerd image list and CRI image list lack that image. Its
45.07-second import had succeeded; the launcher Pods subsequently tried the
external registry and exhausted their readiness waits with `ImagePullBackOff`.

The cleanup predicate selects only untagged, unpinned, unused IDs. Its selection
therefore preceded visibility of the new pinned reference in CRI's asynchronous
cache. By removal time, that reference existed and removal by ID deleted it too.
The earlier repair checks eligibility after removal; it cannot protect a new
reference from the removal itself. Import and cleanup still need coordination
that makes this destructive race impossible, verified by a regression test and
an uncached full VM run.
