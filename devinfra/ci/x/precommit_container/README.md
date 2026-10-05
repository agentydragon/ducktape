# GitHub pre-commit container spike

This experimental branch compares the existing GitHub-hosted Nix setup with a
prepared GitHub job container. It does not switch the required pre-commit check.

`image.nix` combines a digest-pinned Ubuntu base (for GitHub's mounted Node
runtime) with the repository's exact `.#precommit` closure, Git, Python, and CA
certificates. `build.sh` builds and publishes a candidate to GHCR. Registry
credentials remain outside the Nix build.

The branch-only workflow runs three fresh hosted jobs per variant, on the same
source revision and the same four YAML/Nix/Rust files, including Galaxy role setup.
GitHub job and step timestamps supply the measurements. Image construction,
publication, evaluation in the preparation job, and runner assignment waits are
separate from the measured job durations. This is a focused workload, not
validation of every hook or an estimate of whole-PR feedback.

Setting `SPIKE_IMAGE` in the build step reuses a published candidate for harness
reruns; leaving it unset builds a new image. The preparation job
evaluates the current source's Nix closure; candidate jobs compare it with the
closure embedded in the image and fail on mismatch. This guard prevents this
spike from silently accepting stale tools; a production path still needs an
image selection/build policy for PRs that change tooling.

Container jobs explicitly trust only their runner-mounted Git workspace because
checkout's temporary Git configuration is not retained for subsequent hooks.
The container includes zstd so it can restore the existing Galaxy cache using
the same codec as the standard runner.

The first trim copied only the pinned `rustfmt` executable, byte-identically,
into the focused pre-commit package. The current variant also copies its
`librustc_driver` and `libLLVM` shared libraries and relocates their RUNPATHs.
It validates the exact six embedded compiler diagnostic source filenames before
normalizing only their compiler store-path reference. Any other matching string
fails the build. Nix also rejects the result if its runtime closure still contains
the original formatter, compiler, or LLVM output. This is a version-specific
spike: a toolchain update may require reviewing those explicit guards.

Full Rustfmt, including `cargo-fmt`, remains in the developer tool environment.
The preparation job pushes the focused closure to the public Attic cache before
measurements, so baseline jobs substitute it instead of fetching all build-time
dependencies. Only this branch's preparation job receives the cache publishing
credential; measured jobs use anonymous cache reads.

A copy-and-relocate attempt without diagnostic-filename normalization retained
the compiler output and its LLVM dependency. It is not the current variant.
Compiler source filename strings were the identified reference that required
normalization; no other references are removed.

Results and timing evidence belong in the latest HTML report on the dedicated
[`ci-latency-history`](https://github.com/agentydragon/ducktape/tree/ci-latency-history)
branch. This branch is an experiment, not a production rollout proposal.

The runtime trim was compared with the original formatter on all 482 tracked
Rust files using `--emit stdout`; stdout, stderr and exit status matched for
every file. A malformed-input check also matched. Compiler-internal crash
diagnostics can differ because their embedded source paths are normalized.
This is not proof for every formatter option or failure mode.

The workflow now reuses the published runtime-trim digest for a later batch of
three fresh runners. This checks for a registry warming effect without changing
the image or requested hook workload. The original full image had already been
pulled six times across two batches; their median initialization times were
83s and 84s. Download and extraction overlap, so log measurements after the
last download are only a remaining processing tail, not total extraction time.
