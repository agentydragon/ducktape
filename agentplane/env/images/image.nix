# The native harness image: the sandbox image's definition (<./sandbox.nix>) plus nixpkgs' Claude
# Code and Codex, and with `extraPaths` the repository's own hook/build tools. Published as
# environment-tools by .github/workflows/agentplane-environment-tools-image.yml and run by the runner
# SandboxTemplate (cluster/cdk8s/agentplane/environment_sandbox.py), which names the harnesses by their
# /bin paths here.
#
# Runtime, Harness Host, Execution service and the `agentplane` command are not in this image: the
# template mounts them from the environment disk image (//agentplane/env/images:disk_image) at
# /run/agentplane/environment and starts Runtime there, so a service or CLI bump republishes that small
# image and not this one. PATH ends with the mount's
# bin so the harnesses find `agentplane`.
#
# The harnesses are nixpkgs' versions, not the ones //agentplane/env/e2e/... pins and tests, and
# nothing runs a turn through this image before it publishes: after a change that moves either
# harness, run the `_live` targets in //agentplane/system_tests against the deployment.
#
# Build:  nix build .#environment-tools-image
# Load:   docker load < result
{
  pkgs,
  pkgsUnstable,
  name,
  extraPaths ? [ ],
}:
import ./sandbox.nix {
  inherit pkgs name;
  extraPaths = [
    pkgsUnstable.claude-code
    pkgsUnstable.codex
  ]
  ++ extraPaths;
  extraPathEntries = [ "/run/agentplane/environment/bin" ];
}
