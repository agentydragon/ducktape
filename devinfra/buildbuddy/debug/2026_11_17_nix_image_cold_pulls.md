# Experimental Nix image cold pulls through Zot

On 2026-11-17 a first pull of the experimental Nix BuildBuddy image timed out
on the self-hosted `kvm-vm` pool. This is the pool's Docker action image path,
not the hosted Firecracker outer runner. Its executor rewrites GHCR image names
to the in-cluster Zot mirror in `devinfra/buildbuddy/kvm/worker/executor.yaml`.

The [first probe](https://app.buildbuddy.io/invocation/55e4c6ac-fd80-4988-8902-52332249b0dd)
used digest `sha256:7ff3661d85a9245eb5306a0423e433c3d0997f71607b31a982baa5e224601f6b`.
Its 10 registry layers were uncompressed, totaling 4,772,034,560 bytes. The
worker started at 14:41:59 UTC and its image pull was stopped after 5m0.126s,
before input fetch or test execution. Zot logged the manifest request and
on-demand sync beginning at 14:42:01; it finished syncing at 14:46:43 and
returned that manifest after 4m42s. Layer transfer to the worker was then
interrupted. A retry used the already-synced mirror and got through preparation.
[BuildBuddy's current configuration reference](https://www.buildbuddy.io/docs/config-all-options/)
documents a five-minute default for `executor.image_pull_timeout`, which the
worker configuration leaves unset. The observed cutoff is consistent with
that default; the reference has not been checked against the deployed executor
version. Its configured 24-hour task limit is a separate setting.

A temporary publication converted the same image to OCI gzip layers through
Skopeo's OCI-layout staging, as used by the production runner image build. The
resulting digest was
`sha256:5846a238a064a1f89f739c49611e5db9ee0ca56d30464f4f4d9729c8af8a8321`;
its 10 compressed layers total 1,996,245,155 bytes, 58% less. The decoded image
configuration, including root filesystem diff IDs, matched the source. The
config blob digest changed during Docker-schema2 to OCI serialization.

The [compressed probe](https://app.buildbuddy.io/invocation/adee199c-d0d4-45b9-b3e7-b75e20d8015a)
passed on its first cold pull, on a different worker. Zot's on-demand sync took
2m33s; worker preparation from worker start to input-fetch start took 4m06.7s.
The paired numbers are single observations under different conditions, not a
causal estimate or a CI wall-time saving. Four minutes still leaves little
headroom under the five-minute pull timeout.

The immediate bottleneck in the failed pull was Zot synchronizing the entire
image before returning its manifest, which consumed nearly the whole timeout.
Compression is a promising way to reduce the bytes moved; repeat cold trials
before relying on it. Also evaluate prewarming the immutable digest in Zot
before scheduling a test. Neither observation justifies increasing the timeout
without measuring the effects on queue occupancy and failures. No production
image or pin was changed by these probes.
