# sshpiper Pipe CRD

`sshpiper-source` fetches the upstream `plugin/kubernetes/crd.yaml` from the same `v1.6.1` tag as
the [sshpiper deployment](../agents/public-coder-agent/sshpiper/). Its source ignore rules retain
only that CRD, so the upstream sample Pipe is not applied. Flux creates a `kustomization.yaml` for
the plain-YAML source path.

The cdk8s `Pipe` binding reads the same upstream file through the SHA-256-pinned
`sshpiper_pipe_crd` Bazel repository in the root `MODULE.bazel`; the CRD is not copied into this
repository. When upgrading sshpiper, update the image, Flux tag, and Bazel URL and digest together.

The CRD is managed separately from the consumer because the `sshpiper-crds` Kustomization must
outlive individual sshpiperd deployments. Pruning it would delete every `Pipe`. The only consumer
today is <../agents/public-coder-agent/sshpiper/>.
