# Compressed K3s guest root, 2026-10-31

The test guest's EROFS root is imported before the VM scenarios start. Containerd may deduplicate
content already present in its store. Earlier profiling measured a 32.1-second root-import phase in
one run and sampled containerd's blob writes during import
([process-trace collection](process_trace_collection.md)). This experiment enabled LZ4 only for the
K3s test guest's `root_erofs`; the `erofs_image` default remains uncompressed.

## Capability and image output

The fixture uses Debian `linux-image-6.12.63+deb13-cloud-amd64` (SHA-256
`35da0f2338242760a9dcde72340bf063601c1a8507402ddf3e88bf154fbc1d06`). The generated initramfs
contains that exact kernel's `erofs.ko`, whose symbols include `LZ4_decompress_safe`. The pinned
`@trixie_erofs_utils` closure reports `mkfs.erofs (erofs-utils) 1.8.6` and lists `lz4` as an
available compressor. The rule retains `--all-root`, `-T0`, and the fixed all-zero UUID; the image
build action succeeded with `-z lz4`.

| Image             | Source                                            |       Bytes | SHA-256                                                            |
| ----------------- | ------------------------------------------------- | ----------: | ------------------------------------------------------------------ |
| Uncompressed root | `ff579d3582ac18a76a8d4963ccd8a1e1da88414f`        | 906,592,256 | `dba8e34dd0e99a2d0e2d06a61a0a13c03891fbe703de06b1c3b5a8691db82cbd` |
| LZ4 root          | Same source plus guest-only `compress_lz4 = True` | 552,828,928 | `a1b985aadcfca5e214a798fa9fb88421bc7d4626e96c09807032cd8fe0c288fa` |

The candidate image is 353,763,328 bytes (39.0%) smaller. The baseline image came from the cached
`root_erofs` action; the candidate action executed remotely. Both preserve the rule's fixed metadata.
The candidate's recorded SHA is the output of that single build; a separate forced second build was
not completed, so cross-execution byte reproducibility is not claimed here.

## Fresh VM runs

Both runs used `bbr test //agentplane/system_tests/suites:test_vm --nocache_test_results`, with normal
RBE and action-cache settings and the same target executor properties from
[`k3s/defs.bzl`](../defs.bzl): `dockerNetwork=bridge`, 4 estimated compute units, and 50 GB estimated
free disk. The baseline invocation is commit `ff579d35`; BuildBuddy labels the candidate invocation
`a7685d0f` with the local compression patch applied. Between those commits, no files under
`agentplane/system_tests/k3s/` or `agentplane/system_tests/k3s/guest/` changed. The generic system-test
readiness wait and runner-credential wait did change, and artifact pins were refreshed, so these are
not identical whole-test source trees. The PR now uses base `983281cb8e`. Since the measured candidate
commit, `fixture_chart.py` also set the central sampler fixture to one replica. That fixture change is
part of the final test environment and was not in the measured candidate run. The root filesystem
inputs and kernel stayed the same; later staging image and artifact-pin updates do not touch the guest
root. Each test created a new fixture stack; physical executor workers and their cache state were not
pinned to match.

The candidate VM test used the earlier string-valued compression attribute (`compression = "lz4"`).
The final rule uses the typed `compress_lz4 = True` attribute; both produce the same `mkfs.erofs`
arguments, including `-z lz4`. A build with the final boolean API succeeded as invocation
`bbca4cb9-fc08-433f-84e7-600f8d4d4e80` and produced the same 552,828,928-byte image SHA-256 shown
above from cache. The full VM suite has not been rerun after that API cleanup and rebase.

BuildBuddy invocation summaries show baseline 12 action-cache hits, 0 misses, 1,362 CAS hits, and
about 1.47 GB downloaded; candidate 6 action-cache hits, 6 misses, 4,227 CAS hits, and about 4.82 GB
downloaded. The candidate also had one Firecracker pause attempt fail with `fc.sock: connection
refused`; its retry completed successfully. These counts and the distinct physical workers/cache
state are evidence of a heterogeneous comparison, not controlled executor conditions.

| Measurement                       | Uncompressed baseline |      LZ4 candidate |
| --------------------------------- | --------------------: | -----------------: |
| Bazel test action                 |       631.3 s, FAILED |    365.0 s, PASSED |
| Stack ready                       |               285.8 s |            159.9 s |
| Guest-root import                 |                72.1 s |             29.5 s |
| `vm.case` span                    |               306.1 s |            182.7 s |
| VM sandbox create-to-ready median | 124.7 s (121.1–146.0) | 80.0 s (75.2–93.6) |

The baseline scenario body completed, but teardown entered `crictl rmp --all --force` shortly before
pytest's 600-second deadline; pytest reported one pass and one teardown error, so the full baseline
target failed. Its stack-ready plus `vm.case` spans total 591.9 seconds, leaving little time for
cleanup. The candidate completed the full target in 365.0 seconds. The single failed Firecracker
pause attempt and successful retry are recorded above.

Both artifacts contain the six independent VM scenarios: central sampler, Claude and Codex runner
mTLS, Claude and Codex turns, and Codex tool OOM. In both OOM results, the executor limit was exactly
96 MiB (`100663296` bytes), `oom_kill` increased by one, the allocation command exited 137, and the
follow-up command exited 0 with `VM_OOM_FOLLOWUP_OK`. The runner boot ID, process ID, and session ID
match before the kill and after the follow-up. The candidate OOM artifact records `passed: true`.

The root image's own import fell by 42.6 seconds in these runs. The stack-ready delta was larger
(125.9 seconds), and the pre-root fixture-image phase also differed substantially (208.3 s baseline;
120.4 s candidate). VM scenario spans were broadly faster in the candidate run as well. These
single-run differences therefore do not isolate all stack or scenario savings to EROFS compression.
The paired runs establish a promising root-image and root-import result, not a fleet estimate.

No guest-side CPU or EROFS decompression profile/counter was collected. The 15-second containerd CPU
profiles sampled 8.98 seconds baseline and 10.58 seconds candidate; `content.WriteBlob`/archive
import accounted for 2.08 and 3.37 cumulative sampled seconds, respectively. These host-side profile
windows were not phase-aligned to the guest-root import and do not observe guest decompression CPU,
so they cannot establish CPU regression or its absence. The VM create-to-ready span includes
scheduling and launcher work; it is not a direct kernel-boot measurement.

## Invocations and artifact hashes

- [Baseline root image build](https://app.buildbuddy.io/invocation/3cee4447-2ce3-473e-9982-387298e884b9)
- [Baseline VM test](https://app.buildbuddy.io/invocation/5b93ed79-1c48-444d-aee9-46f1602e6ddd)
- [Candidate root image build](https://app.buildbuddy.io/invocation/c8c3167f-50d0-4deb-9d9e-740cb95eaf3a)
- [Candidate VM test](https://app.buildbuddy.io/invocation/f94c1a29-7067-4f59-97e6-41e14cc0d05f)

| Artifact                                       | Baseline SHA-256                                                   | Candidate SHA-256                                                  |
| ---------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------ |
| `test.outputs/k3s-timeline.jsonl`              | `28b3a866ed9fe640cd3c05effe1a53900bc4ac0e4cefebcf977b6370d9667a11` | `7cd07056bd98dfd9e95c144c7cd36470a9d8ef9f8caaa48f9c42a645adab7b81` |
| `test.outputs/vm-phases.jsonl`                 | `59aef906cc84540cb79c11e5cf691f4fcc72b6ca67d792e4a02556b4501b1c0a` | `49d33ea04651a318affc85166dde474114ca195e8e03aeeb7b0118a262782572` |
| `test.outputs/vm-codex-oom.json`               | `63d0beaccb6b87eea4753bcd5ca9972e0799244c16200170ae6473a31c9e03c0` | `2cd2edb900c9cc3062c4b3becc61d82084117bc832e2478a22deac7a0585d3f0` |
| `test.outputs/containerd-import.cpu.pprof`     | `a36224a17f41a4fb57e10e65dd0fc0264329a35147ea8d9c306c9ec2aa568704` | `da8252fbd9e894ed7247ab85bc40adcd211d4f26c1476a7543356459ef9dc3c2` |
| `test.outputs/containerd-import.runtime.trace` | `b33eb8862d941cbd3e554fb70dae39f4d56e591939c29a0ddcfb0007b1b0051c` | `c1f7afca6beefe4f8bbf02ad78165fda32121a0122ae5669cf1baba8b0370aaa` |

These are one run per arm with the same declared RBE executor properties, but different source
revisions, physical workers, and cache state. The image shrank and its measured root-import span fell
in this sample; the full-suite delta does not establish a CI-wide speedup, and the available profiles
do not establish CPU non-regression. No repeated full-suite sample was collected.
