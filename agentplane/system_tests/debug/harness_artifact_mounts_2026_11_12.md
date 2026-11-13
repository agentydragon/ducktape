# Direct harness artifact mounting

This manual experiment tracks [#16030](https://github.com/agentydragon/ducktape/issues/16030).
Required suites retain ordinary OCI packaging. The candidate removes only the Claude/Codex archive
layers from the fixture environment-tools image, preserving its other tools, command links and UID.
It declares the pinned Claude executable and Codex file tree directly as Bazel test inputs.

K3s runs directly on the RBE action's node, so its Pod workloads can bind those declared input files
through read-only hostPath volumes. Every file is mounted at its ordinary `/opt/harnesses/…` path
in both Environment Runtime and the isolated Codex executor. Binding each resolved file avoids
absolute runfiles/file-cache symlinks dangling inside the Pod root. This is a fixture boundary;
it does not provide a shared production filesystem or a transport into VM guests.

The manual targets `//agentplane/system_tests/suites:test_harness_artifacts` and
`//agentplane/system_tests/suites:test_harness_packaging` run the same real Claude/Codex scenario.
Each creates two fresh Sandboxes with ordinary home/state volumes and checks the fresh writable
home, executable paths and versions. The mounted arm also checks the native mount flags are read-only.
Each harness executes two real Session turns against the scripted model and checks standing
instructions reach only the intended Session. The packaged arm boots the ordinary image and runs
the same turns, retaining a real image packaging/boot check in this experiment.

## Measurement boundary

The candidate's RBE inputs contain raw artifact files, while the control contains their OCI layers.
Compare actual input-root identities, bytes downloaded and input-fetch time alongside containerd
imports, stack setup, real turn time, cleanup, CPU and peak memory. Declaring a source file is not a
free cache hit. A paired invocation can establish feasibility and expose physical costs; different
workers, warmth and competing load prevent causal speedup attribution.

Required CI coverage is unchanged. Adoption requires a matched counterbalanced full selected
Pod/MCP Actions/VM graph, including a required packaged-image boot path, and preservation of source
pins, permissions, readonly mounts and fresh mutable home/state across later Sandboxes. Retain
separate VM guest packaging. Revisit on the next relevant backend head or within seven days after
a manual feasibility pass; close #16030 only after that graph and required-gate/resource comparison
supports a concrete policy decision.
