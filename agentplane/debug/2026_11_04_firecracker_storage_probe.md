# Firecracker production image observations

These measurements cover the production Nix guest and launcher image. The regular
`//agentplane/system_tests/suites:test_firecracker` target uses a separate, smaller Debian
guest, so its pass does not establish production tool parity or production image size.

## Production disk and copy observations

The production disk and worker-profile observations below came from source
`d110313300cc63e59f7b36cf514de06e57763adc`.

The production Nix root image was 9,073,119,232 bytes logical and 6,732,808,192 bytes allocated
at build output. On the worker, container image import materialized the sparse holes
(9,073,123,328 allocated); launcher preparation recovered them, leaving 6,665,912,320 allocated
after boot writes. The 2 GiB state disk used 69,914,624 bytes and the 8 GiB workspace used
73,715,712 bytes. Allocations are `stat.st_blocks * 512`, not guest filesystem usage.

On a warm host filesystem, sparse-copying the 9.07 GB root with
`cp --reflink=auto --sparse=always` took 11.183 seconds elapsed. This excludes the launcher's
explicit sync, manifest publication and other checkpoint files, and is not a worker checkpoint
latency measurement.

The worker profile mixed setup and lifecycle work, and profiler cache activity filled its small
root disk. It is unsuitable as a latency baseline. Future profiles should keep their cache and
outputs on the work disk and isolate the copy/sync phase.

## Smaller production Nix guest

An isolated NixOS `extendModules` variant set `preCommitPackages = []` while retaining the other
guest modules, normal tools, harnesses and kernel settings. Its system closure fell from 893 paths
and 6,459,778,504 NAR bytes to 618 paths and 2,120,167,576 NAR bytes (67.2%). The assembled root
was 4,086,632,448 bytes logical and 2,613,149,696 allocated; after boot it used 2,548,985,856
allocated. The baseline production root was 9,073,119,232 logical and 6,665,912,320 allocated
after boot. NAR and disk size are not RAM or latency measurements, and omitting pre-commit tools
does not establish parity for arbitrary workloads.

The [hardware probe](https://app.buildbuddy.io/invocation/88af37ca-0bda-4bfc-a9d7-0883c1840506)
used runtime source `3ecae3a864` and the smaller production Nix guest. It passed both scripted turns,
mount-type checks, projected-token absence and RAM/process continuity in 959.2 seconds, including
129.575 seconds of cleanup. This was a separate full target run, not the regular Debian-guest CI
test or a controlled comparison against the production guest. The production image pin and guest
configuration were unchanged.
