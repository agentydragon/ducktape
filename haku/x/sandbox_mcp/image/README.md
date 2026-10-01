# Parked Haku Console sandbox image

This image was the exec target for the retired Haku Console Sandbox MCP. It is preserved for
reference under `haku/x/sandbox_mcp/image/`; neither variant has an active deployment consumer, and
the build workflows are archived beside the image source.

| Source        | Former publish workflow                | Former registry image                |
| ------------- | -------------------------------------- | ------------------------------------ |
| `Dockerfile`  | `workflows/haku-sandbox-image.yml`     | `ducktape-ci/haku-sandbox-image`     |
| `default.nix` | `workflows/haku-sandbox-image-nix.yml` | `ducktape-ci/haku-sandbox-image-nix` |

Both variants use `haku-sandbox-setup.sh`. The old Nix probe passed 25/26 tests in a probe Pod;
only `//ui/e2e:test_e2e` failed because the image had no Docker socket. Bazel's downloaded FHS
helpers required nixpkgs' Bazel, nix-ld's filesystem fallback, and the `bazel-shell` wrapper. The
reusable NixOS details remain in [`devinfra/debug/nixos_bazel_bash`](../../../../devinfra/debug/nixos_bazel_bash/README.md).

## Former runtime contract

The former Haku `SandboxTemplate` ran an idle shell as an exec target. Its pod command overrode the
image entrypoint, so the Nix variant's `tini` did not become PID 1; a restored runtime would need to
review that pod shape. The bootstrap wrote the egress CA trust, Git identity and `.netrc`, then
shallow-cloned `haku-state` and `ducktape`.

The old Nix cutover checklist and probe procedure are superseded. The Haku-specific template, warm
pool, image pin, and ImagePolicy are no longer active. Agentplane-managed Haku sandboxes use their
own runner image and inline bootstrap from `cluster/cdk8s/agentplane/haku_bootstrap.sh`.

Do not revive this image as part of the Agentplane migration. Any future use needs a deliberate
sandbox identity and access design plus a fresh deployment review.
