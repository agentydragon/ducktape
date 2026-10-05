# BuildBuddy remote runner image

The outer `bb remote` runner adds the Nix `buildbuddy-remote-runner-tools`
package to the pinned RBE base image. `.github/workflows/container-images.yml`
builds the real Dockerfile on pull requests without publishing it.

On branch builds, `devinfra/ci/runner_image.py plan` compares the pinned runner's
`works.allegedly.ducktape.runner-inputs` label with a hash of:

- The evaluated Nix package output path (including its transitive inputs).
- The RBE base image reference and target platform, `linux/amd64`.
- The Dockerfile, `.dockerignore`, shared build action, workflow, identity helper,
  and Attic public keys retained in the image.

A matching label skips the runner build, publication, and repin. Broad Nix trigger
paths remain: unrelated artifact updates can trigger evaluation without changing
the image. Recipe changes conservatively rebuild, even if they only change a
comment. Older images without the label rebuild once; evaluation and registry
errors fail visibly. FreeCAD can still publish and repin when the runner is reused.

The Docker build receives `BASE_REF`, `EXPECTED_TOOLS_PATH`, and
`RUNNER_INPUTS_KEY` from the helper's `inputs`/`plan` outputs. It verifies the
versioned Nix installer checksum and checks the actual built tool path against
`EXPECTED_TOOLS_PATH`. The pin job reevaluates the identity on its latest checkout
and again after rebasing; changed inputs reject the stale runner pin. A later
concurrent push is rejected by Git's normal non-fast-forward check.

Changing the tool closure, base image, or recipe intentionally produces a new
image. Keeping the outer image stable avoids unnecessary publishing and preserves
one input to BuildBuddy runner reuse; it does not guarantee a warm runner or a
particular CI latency reduction. This does not change the RBE action image.
