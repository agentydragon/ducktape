# Fixture guest image removed during CRI publication

The [KVM suite retry](https://app.buildbuddy.io/invocation/6aa01b69-b754-4261-a885-67b7577f4bd1)
imported the Bazel-built guest-root layout, then all three VM launchers tried to pull its
`git.allegedly.works/ducktape-ci/agentplane-vm-guest-root:unset` fixture tag. The worker's
egress policy did not allow that registry. The CRI image inventory at failure contained the
guest boot image but no guest root tag.

The invocation's `image-prune.log` records `crictl rmi` of config ID
`sha256:52c70eea6cbdb91f5e33a35dfdc35618939db66ab4b020e7c6000e24fca70422`, with output
`Deleted: git.allegedly.works/ducktape-ci/agentplane-vm-guest-root:unset`. Its retained
`containerd.log` shows the ID alias created at 05:56:01.873 UTC, the tag updated with the
pinned label at 05:56:01.874, the `RemoveImage` request at 05:56:02.184, and deletion of the
tag at 05:56:02.815. The launcher attempted a registry pull at 05:58:16.

The CRI inventory can expose an ID before its named reference appears. Containerd's
[RemoveImage implementation](https://github.com/containerd/containerd/blob/v2.2.7/internal/cri/server/images/image_remove.go)
removes all references of the resolved image, even when called with its config ID. A
post-removal inventory check cannot repair a tag deleted during that interval. Protecting
current fixture config IDs derived from their runfile layouts before pruning closes this
race; genuinely obsolete IDs remain eligible for cleanup. No registry access is required.
