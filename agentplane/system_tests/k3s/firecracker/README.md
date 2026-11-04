# Firecracker boot layout

`boot_layout` exposes the production Firecracker OCI layout as a declared
Bazel input. The manual K3s Firecracker system test imports it as the boot
volume, so its bytes are available to the RBE test action through runfiles.

The pinned image comes from
`.#agentplane-environment-firecracker-image` at source commit
`ff14e8bd057d51591be67f5d94a085bb5f3f6f91`. The Nix archive SHA-256 is
`a0334c39979b4766cdd8276ca50c7d6ca4cafac9bf5071099b0493a5e6e872e0`; its
OCI manifest is pinned as
`sha256:f1465053413c073aa5aa9c75d4c3e9fa6225021b94f48cc8cceb2a30271a05e1`
from the experimental tag
`spike-firecracker-ff14e8bd057d51591be67f5d94a085bb5f3f6f91`.

The image contains only the built guest kernel, ext4 root filesystem, and init
path. The guest configuration has no SOPS secrets; runtime TLS credentials and
identity arrive through host-provided mounts and virtio devices when it boots.
The experimental tag does not match the managed Flux policy, which selects
`devel-<timestamp>-<commit>` tags only.

The Forgejo package is private. Bazel's pinned `rules_oci` reads registry auth
from the standard Docker config under `DOCKER_CONFIG/config.json`. Run the
manual test from an authorized local Bazel client with an existing read-capable
Docker config, for example:

```sh
DOCKER_CONFIG=/secure/path/to/docker-config bazelisk test \
  --repo_env=DOCKER_CONFIG \
  --nocache_test_results //agentplane/system_tests/suites:test_firecracker
```

The config directory must contain `config.json` and remain mode-restricted.
The Bazel client uses it to fetch the pinned OCI repository; the test and build
actions still use the repository's standard RBE configuration. Do not forward
registry credentials to BuildBuddy runners, put them in command arguments or
logs, or add a persistent registry key for this test. The system test is
tagged `manual` and is excluded from the default CI test selection.

To refresh the pin, publish the same Nix output through the existing
`Agentplane Environment Firecracker Image` workflow, read the resulting image
manifest digest, and update `devinfra/image_pins.json`. Keep the tag and digest
paired so the source artifact is traceable and immutable to Bazel.
