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

The next comparison runs one selected arm per action with the same immutable
inputs and pre-read treatment, requesting no runner recycling. Retain cold and
warm postconditions, physical action identities, source/route metadata and all
failures. Compare import work separately from startup, pre-read, teardown, action
queue and complete required feedback. Preserve the current production importer
until the isolated result supports a useful change. Raw artifacts stay private;
[#16099](https://github.com/agentydragon/ducktape/issues/16099) tracks qualification.
