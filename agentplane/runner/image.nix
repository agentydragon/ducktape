# The runner image: the sandbox image's definition (<../images/sandbox.nix>) plus nixpkgs' Claude
# Code and Codex, and with `extraPaths` the repository's own hook/build tools. Published as
# runner-ducktape by .github/workflows/agentplane-runner-ducktape-image.yml and run by the runner
# SandboxTemplate (cluster/cdk8s/agentplane/runner_sandbox.py), which names the harnesses by their
# /bin paths here.
#
# The runner, executor companion and `agentplane` command are not in this image: the template mounts
# them from the runner disk image (//agentplane/runner:disk_image) at /run/agentplane/runner and starts
# the runner there,
# so a runner or CLI bump republishes that small image and not this one. PATH ends with the mount's
# bin so the harnesses find `agentplane`.
#
# The harnesses are nixpkgs' versions, not the ones //agentplane/runner/... pins and tests, and
# nothing runs a turn through this image before it publishes: after a change that moves either
# harness, run the `_live` targets in //agentplane/system_tests against the deployment.
#
# Build:  nix build .#runner-ducktape-image
# Load:   docker load < result
{
  pkgs,
  pkgsUnstable,
  name,
  extraPaths ? [ ],
}:
import ../images/sandbox.nix {
  inherit pkgs name;
  extraPaths = [
    pkgsUnstable.claude-code
    pkgsUnstable.codex
  ]
  ++ extraPaths;
  extraPathEntries = [ "/run/agentplane/runner/bin" ];
}
