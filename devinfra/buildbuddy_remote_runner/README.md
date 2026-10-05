# BuildBuddy remote runner image

The outer `bb remote` runner combines the digest-pinned RBE Ubuntu image with
`buildbuddy-remote-runner-tools` (bb, Bazelisk, bazel-diff). Nix assembles the image
from the existing base layers and the tools' runtime closure; it installs nothing
inside a running container. `/usr/local/bin` exposes the tools to BuildBuddy's
runner, while the base filesystem and runtime configuration stay intact.

## Build and verify

Authenticate to GHCR using your normal Docker credentials, then from the repo root:

```bash
nix shell .#runner-image-tools --command python3 devinfra/ci/nix_runner_image.py build \
  --work-dir /tmp/runner-image \
  --image ghcr.io/agentydragon/buildbuddy-remote-runner \
  --check-reproducible
```

Use a fresh work directory. The helper fetches the base from `image_pins.json` with
Skopeo, imports its archive into the Nix store, and builds
`.#buildbuddy-remote-runner-image`. Registry credentials never enter the store.
The archive store path is passed through `DUCKTAPE_RUNNER_BASE_IMAGE`, so evaluation
uses `--impure`; the image derivation itself uses only declared store inputs.
There is no separately maintained archive hash or image-input label.

Image names, tags, timestamps, ownership, and compression are fixed. Repeated
assembly checks both Nix's output reproducibility and the resulting OCI manifest
digest. This checks image assembly; it does not rebuild every tool dependency from
source. The tools, base bytes, image configuration, and assembly tooling determine
the result. Unrelated repository revisions and publication timestamps are excluded.

`dockerTools` inherits only the base environment. The recipe explicitly carries
its command and labels; the helper compares the entire runtime config, OS, and
architecture with the imported base and fails if a future base requires an update.

## Publication

`.github/workflows/buildbuddy-remote-runner-image.yml` owns this image. PRs build it
twice and exercise the tools in Docker with read-only registry access. Branch runs
publish only when the assembled manifest differs from the current pin. Skopeo
preserves the manifest digest during upload; the helper checks the registry result.
Changed candidates must also pass a real BuildBuddy test invocation with Docker
initialized before pinning. Manual dispatch defaults to candidate-only validation
without changing a branch pin. The pin job checks the current base and image derivation before changing the pin
and again after rebasing. A concurrent advance after that check rejects the push.

FreeCAD and other standalone images keep their separate container-images workflow.
The RBE action image, its trigger, and its pin are unchanged by this migration.
A real BuildBuddy runner invocation remains necessary when changing the image's
runtime contract: Docker smoke tests alone do not exercise Firecracker or goinit.
