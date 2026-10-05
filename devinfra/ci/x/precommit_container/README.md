# GitHub pre-commit container spike

This experimental branch compares the existing GitHub-hosted Nix setup with a
prepared GitHub job container. It does not switch the required pre-commit check.

`image.nix` combines a digest-pinned Ubuntu base (for GitHub's mounted Node
runtime) with the repository's exact `.#precommit` closure, Git, Python, and CA
certificates. `build.sh` builds and publishes a candidate to GHCR. Registry
credentials remain outside the Nix build.

The branch-only workflow runs three fresh hosted jobs per variant, on the same
source revision and the same three YAML/Nix files, including Galaxy role setup.
GitHub job and step timestamps supply the measurements. Image construction,
publication, evaluation in the preparation job, and runner assignment waits are
separate from the measured job durations. This is a focused workload, not
validation of every hook or an estimate of whole-PR feedback.

The checked-in `SPIKE_IMAGE` reuses the published candidate for harness reruns.
Remove that environment variable to build a new candidate. The preparation job
evaluates the current source's Nix closure; candidate jobs compare it with the
closure embedded in the image and fail on mismatch. This guard prevents this
spike from silently accepting stale tools; a production path still needs an
image selection/build policy for PRs that change tooling.

Container jobs explicitly trust only their runner-mounted Git workspace because
checkout's temporary Git configuration is not retained for subsequent hooks.
The container does not contain zstd, so Galaxy cache restore differs from the
standard runner; include it before any broader cache comparison.

Results and timing evidence belong in the latest HTML report on the dedicated
[`ci-latency-history`](https://github.com/agentydragon/ducktape/tree/ci-latency-history)
branch. This branch is an experiment, not a production rollout proposal.
