# BuildBuddy and Bazel terminology audit

This is a follow-up backlog. The accompanying PR aligns the names and paths for the
BuildBuddy Remote Runner image and the RBE container image; these are other references
that could be cleaned up separately.

## Terminology baseline

- **BuildBuddy Remote Runner** runs Bazel commands in a remote workspace. BuildBuddy
  describes it as an execution environment running on a BuildBuddy executor.
- **RBE executor** runs Bazel's remote actions. The action's environment can use a
  custom **RBE container image**, selected in BuildBuddy with the `container-image`
  execution property.
- **Bazel `exec_properties`** belong to an execution platform. Bazel forwards the
  string map to the Remote Execution API without interpreting its values.

Sources: [BuildBuddy Remote Runners](https://www.buildbuddy.io/docs/remote-runner-introduction/),
[BuildBuddy RBE Platforms](https://www.buildbuddy.io/docs/rbe-platforms/), and
[Bazel platform `exec_properties`](https://bazel.build/reference/be/platforms-and-toolchains).

## Follow-up candidates

### A. [DOC] Replace "RBE worker image" when the text means the action container

Evidence:

- `devinfra/ci/skills/cihealth/SKILL.md:92-93` calls the action-cache input the
  “RBE worker digest” and “RBE worker image.”
- `docs/buildbuddy_selfhost.md:59` calls custom action containers “RBE worker
  images.”
- `devinfra/ci/docs/publish_planning.md:158` says “RBE worker image” when explaining
  why the image build does not depend on its own published artifact.
- `devinfra/pr_visuals/README.md:96` and
  `devinfra/ci/plans/bazel_diff_base_hash_caching.md:173` call a pin change an
  “rbe-worker” / “RBE image” bump.

Why: the `container-image` property carries the full OCI reference, including the pinned
digest, and that platform value participates in remote action keys. “Worker” can
instead mean the executor machine that hosts those actions.

Proposed change: use “RBE container image” for the pinned OCI image and its digest;
reserve “executor” or “worker” for the machine or service that runs actions.

Verification: review these references in context and confirm each image-pin/cache-key
statement names the container image.

### B. [INV] Check old GHCR references and tags in runnable examples

Evidence:

- `docs/buildbuddy_selfhost.md:273` configures `container-image` with
  `ghcr.io/agentydragon/rbe-worker`.
- `devinfra/buildbuddy_cli/skills/buildbuddy_api/SKILL.md:241,246,251` uses the
  `rbe-worker:nix-devtools` and `rbe-worker:latest` tags in `bb execute` examples.
- `x/bb_box/README.md:19` uses `rbe-worker:nix-devtools`.
- `nix/home/claude_code/default.nix:611` describes `rbe-worker` as the BuildBuddy
  RBE image.

Why: the PR's publishing workflows move the maintained GHCR names to
`rbe-container-image` and `buildbuddy-remote-runner`. The examples may therefore
stop naming the published artifact; the `nix-devtools` tag may also be a separate,
intentional image variant.

Proposed change: verify which examples are still used and whether each tag exists.
Update active examples to the new published name and a tag the workflow actually
publishes; preserve dated observations as historical records.

Verification: inspect the published GHCR manifests/tags after the renamed workflows
run, then check each surviving command against those tags.

### C. [DONE] Name the runner package output after its consumer

The flake output is `buildbuddy-remote-runner-tools`; the
`devinfra/buildbuddy_remote_runner/Dockerfile` installs it into the BuildBuddy Remote
Runner image.

Why: the package serves the Bazel client running on the BuildBuddy Remote Runner. The
output name distinguishes it from tools installed in the RBE action container.

Verification: repository search confirms that the flake output is used only for the
remote runner bundle.

### D. [INV] Clarify the `bb_runner_probe` name and schema owner

Evidence: `devinfra/ci/bb_runner_probe.py:1,25` describes a probe for
`bb remote` runner reuse but uses the module/schema name `bb_runner_probe`.
The filename, Bazel targets, and test use that same short name in
`devinfra/ci/BUILD.bazel:93-97,168-171,241-247`.

Why: `bb_runner` is less explicit than “BuildBuddy Remote Runner,” and the probe
records the outer Bazel runner VM rather than an RBE action executor.

Proposed change: first establish whether the `ducktape.bb_runner_probe.v1` schema or
probe artifacts have consumers outside this repo. If they are internal, consider
renaming the module, targets, test, and schema atomically to
`buildbuddy_remote_runner_probe`.

Verification: search code and stored probe artifacts for schema consumers before
changing the identifier; after any rename, search for the old identifier across the
repo.

## References to preserve

- `devinfra/image_pins.json` and `devinfra/bbr.json` keep their current GHCR
  repositories paired with their current digests until each renamed publishing
  workflow publishes and re-homes its pin. These are intentional transition values.
- `debug/2026_08_rbe_small_test_timeouts.md`,
  `devinfra/rbe_container_image/docs/firecracker_docker_init_timeout.md`,
  `devinfra/claude/testing/INVESTIGATION_ci_bad_length.md` (which quotes its
  historical trigger commit title), and `devinfra/ci/debug/ci_latency_evidence.json`
  record historical image tags, commit titles, or workflow names. Keep those
  observations verbatim.
- Uses of “RBE worker” for the executor machine or its Docker/display capabilities,
  such as `AGENTS.md:63`, describe a different role and should not be globally
  replaced.
