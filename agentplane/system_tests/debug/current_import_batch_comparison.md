# Current-source image import comparison

The manual `//agentplane/system_tests/suites:test_import_batch_benchmark` compares
the current production serial importer with an isolated 8-image/256-MiB batching
candidate. Both preserve usable-image skips, protected OCI configs, pinned tags,
low-disk pruning and exact unpacked target checks. It owns ordinary
`/var/lib/k3s-env` stores inside its action and uses no ordinary suite snapshot key.

At base `d642132a68` with the benchmark working-tree patch, two NEW physical
actions pass one nonempty JUnit case each, without failures, errors, skips or
retries. The compiled Pod manifest has 17 references, 96 unique blobs and
1,789,698,529 source bytes. Both runs have identical input identities. The source-only
16-image estimate missed a declared image; neither that estimate nor the held
18-image benchmark defines these measured inputs.

| Invocation                                                                                 | Cold serial | Cold batches | Warm serial | Warm batches |
| ------------------------------------------------------------------------------------------ | ----------: | -----------: | ----------: | -----------: |
| [serial first](https://app.buildbuddy.io/invocation/51c4b1c4-4142-4606-a583-8bea5ac43b2b)  |     76.974s |      29.787s |      2.381s |       1.330s |
| [batches first](https://app.buildbuddy.io/invocation/70230918-0aa3-4ac5-bb93-2f8170b430ef) |     27.000s |      60.064s |      1.348s |       1.390s |

These are enclosing import spans, not complete-CI timings. Every cold phase has
zero preexisting source blobs; every warm repeat retains all 96. After every
phase and prune, all 17 CRI references have the expected pinned config ID and
exact unpacked target digest. Source layouts are read before each phase.

The second cold store is much faster in both directions. Even the large harness
image, imported alone by both algorithms, falls from 32.481s to 14.084s in the
serial-first action and from 30.200s to 9.811s in the batches-first action. The
7.062s difference between the two algorithm means does not isolate batching from
this order interaction. The mechanism behind the remaining first-arm cost is
unproved. No production importer change or standalone speedup PR is justified yet.

## Isolated actions and bootstrap interference

The next serial/bounded/bounded/serial screen selected one algorithm per action
and requested no runner recycling. Source tree `79f7b3cb07` was uploaded as a
patch over the invocation-reported base `d8258d6991`. All four actions used the
same 17-image/96-blob input identity
`44daa4e81c2d37fb70a9d70d063291e838487b3fac47fb949f01801221218fdd`,
which differs from the earlier two-arm cohort. Three actions passed; the second
bounded action failed its cold-store precondition before either import phase.

| Arm        | Invocation                             | Cold import | Outcome                        |
| ---------- | -------------------------------------- | ----------: | ------------------------------ |
| Serial A1  | `ee100665-bca3-4eea-ab8d-ed46fa1a60fb` |     91.678s | Pass                           |
| Bounded B1 | `a5f6c507-4397-42de-ba05-3bde73c4b88d` |     87.190s | Pass                           |
| Bounded B2 | `38e5f964-33ca-4bcc-874e-fca199889fdd` |  Unmeasured | Cold-store precondition failed |
| Serial A2  | `5df404ce-4c68-460f-ad6f-2cebe1a81e04` |     72.360s | Pass                           |

K3s returns after NodeReady while built-in add-ons can still pull images. The
failed action's containerd log shows a concurrent CoreDNS pull; all six source
blobs already present in its store exactly match layers in that public CoreDNS
manifest. This is benchmark bootstrap contamination, not an exposed importer
failure. Passing actions can also overlap asynchronous pulls, so this cohort
does not isolate a useful speed delta.

The benchmark now disables CoreDNS and local-storage explicitly; it starts no
workload Pods. Ordinary suite defaults remain unchanged. At source tree
`bab17eaf52`, uploaded over the same reported base, the corrected Pod/tmpfs pair
retains the same exact input identity and all cold/warm/pin/unpack/prune guards:

| Arm        | Invocation                             | Cold import | Physical action | Native invocation |      CPU |
| ---------- | -------------------------------------- | ----------: | --------------: | ----------------: | -------: |
| Serial A3  | `6549db95-05bb-4783-960e-ba839a658c37` |     69.366s |        118.724s |          149.439s | 273.999s |
| Bounded B3 | `18eff6bb-0bf4-4913-9a9a-5c87659564aa` |     68.760s |        122.950s |          150.943s | 298.898s |

Both actions are NEW, pass one nonempty case each without failures, errors, skips
or retries, and contain no CRI image-pull records. Batching cuts the cold span by
only 0.606s (0.87%), while physical execution is 4.225s slower and measured CPU is
9.1% higher. Stop this Pod probe without a production port; one clean pair is not
a statistical rejection of batching on other profiles.

The manual `//agentplane/system_tests/suites:test_vm_import_batch_benchmark`
instead consumes the required VM suite's complete manifest with a disk-backed
store and production-order groups. It starts no guests or workload Pods. This
separate probe addresses the current required VM tail: CI
`09528edd-001d-530e-bd0a-5a005d05d032` at merge `3691a3a18c` recorded 190.477s
across 25 import spans inside 215.780s of setup. These nested phases are not
additive, and an import-only result cannot establish a complete-CI saving.

Preserve the serial production importer until a useful comparison supports a
change. Raw artifacts stay private;
[#16099](https://github.com/agentydragon/ducktape/issues/16099) tracks qualification.

## Completed disk-backed VM screen

Four actions submitted before the optimization pause completed at literal source
`6997d126206312acbeb0bdc9ec559844087942df`, in bounded/serial/serial/bounded
order. Each uses exactly the same compiled input identity
`c8fe1accdb1473d029fc5a860a5dab908b06604b7ac4152d0e54a4a9fa459298`:
25 references (7 VM, 4 PKI, 12 fixture, 2 guest), 135 unique blobs and
2,627,041,492 source bytes. Stores are disk-backed; unused bootstrap add-ons are
disabled and recycling is requested off. Each action passes one nonempty JUnit
case with no failures, errors, skips or retries. All are distinct NEW physical
actions; cold-zero, warm-all, exact pinned config, unpacked target and post-prune
retention checks pass.

| Arm        | Invocation                                                                            | Cold imports | Physical execution | Native invocation |      CPU |
| ---------- | ------------------------------------------------------------------------------------- | -----------: | -----------------: | ----------------: | -------: |
| Bounded B1 | [6f772fa4](https://app.buildbuddy.io/invocation/6f772fa4-88f0-49ed-9ed2-b030a8962c55) |     108.767s |           170.597s |          211.083s | 432.974s |
| Serial A1  | [73c6b935](https://app.buildbuddy.io/invocation/73c6b935-75fc-4092-bc2d-31ed7aa736df) |     131.952s |           212.504s |          249.344s | 531.773s |
| Serial A2  | [a4248865](https://app.buildbuddy.io/invocation/a4248865-bd41-4b6a-9b27-953c20f02303) |     135.012s |           219.779s |          254.261s | 530.875s |
| Bounded B2 | [324d93d6](https://app.buildbuddy.io/invocation/324d93d6-d164-49ad-b54e-8de9a761cf37) |     117.889s |           184.047s |          219.585s | 458.691s |

Mean cold imports fall **133.482s → 113.328s (15.1%)**, physical execution
**216.142s → 177.322s (18.0%)**, and CPU **531.324s → 445.832s (16.1%)**.
Native invocation mean falls 14.5%; that clock also includes outer preparation
and analysis. This is a promising import-only result on four correlated actions,
not a deployed VM scenario, complete selected graph or rerun-inclusive PR result.
The production importer remains serial; a port and real deployed/full-graph
validation have not been performed.

Two earlier actions remain excluded from these means. Serial invocation
`c7297e2a-6283-4d61-a882-dfc7b7648d9f` passed but had a different guest-root
OCI identity, despite equal counts/bytes. Its executed packaging action produced
that differing OCI layer; where the inputs or output first diverged remains unproved.
The other 24 images match, and the four retained actions match completely.
Invocation `ccd77df7-8251-48c4-85bd-46427ce3a76a` failed because the submitted
arm name was invalid; no import phase ran. Neither is erased or counted as an
importer performance outcome.
