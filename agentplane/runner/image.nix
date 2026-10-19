# The runner image: the sandbox image's definition (<../images/sandbox.nix>) plus the released
# static Go runner binary (the `agentplane-runner` pin in nix/artifact-pins.json) and nixpkgs'
# Claude Code and Codex. Published as runner by
# .github/workflows/agentplane-runner-image.yml and run by the runner SandboxTemplate
# (cluster/cdk8s/agentplane/runner_sandbox.py), which names the harnesses by their /bin paths here.
#
# The harnesses are nixpkgs' versions, not the ones //agentplane/runner/... pins and tests, and
# nothing runs a turn through this image before it publishes: after a change that moves the
# runner pin or either harness, run //agentplane/acceptance against the deployment.
#
# Build:  nix build .#runner-image
# Load:   docker load < result
{
  pkgs,
  pkgsUnstable,
  runner,
  name ? "runner",
  extraPaths ? [ ],
}:
import ../images/sandbox.nix {
  inherit pkgs name;
  extraPaths = [
    runner
    pkgsUnstable.claude-code
    pkgsUnstable.codex
  ]
  ++ extraPaths;
  extraConfig.Entrypoint = [ "/bin/agentplane-runner" ];
}
