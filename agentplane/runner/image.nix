# The runner image: the sandbox image's definition (<../sandbox_image/default.nix>) plus the runner,
# installed from its released wheel (the `agentplane-runner` pin in nix/artifact-pins.json),
# nixpkgs' Claude Code and Codex, and what a local Bazel build needs. Published as
# agentplane-runner by .github/workflows/agentplane-runner-image.yml and run by the
# agentplane-runner SandboxTemplate (cluster/cdk8s/agentplane/app.py), which names the harnesses by
# their /bin paths here.
#
# The runner's Python and both harnesses come from nixos-unstable: the wheel's generated protobuf
# modules need protobuf >= 7.36, which nixos-26.05 does not ship.
#
# The harnesses are nixpkgs' versions, not the ones //agentplane/runner/... pins and tests, and
# nothing runs a turn through this image before it publishes: after a change that moves the
# runner pin or either harness, run //agentplane/acceptance against the deployment.
#
# Build:  nix build .#agentplane-runner-image
# Load:   docker load < result
{
  pkgs,
  pkgsUnstable,
  wheel,
}:
let
  python = pkgsUnstable.python314;

  runner = python.pkgs.buildPythonApplication {
    pname = "agentplane-runner";
    version = "latest";
    format = "wheel";
    src = wheel;
    dependencies = with python.pkgs; [
      aiosqlite
      greenlet # sqlalchemy's asyncio extension
      grpcio
      protobuf
      pydantic
      sqlalchemy
      typer
    ];
    pythonImportsCheck = [ "agentplane.runner.main" ];
  };
in
import ../sandbox_image {
  inherit pkgs;
  name = "agentplane-runner";
  extraPaths = [
    runner
    pkgsUnstable.claude-code
    pkgsUnstable.codex
    # A local Bazel build compiles and probes with these: rules_cc's auto-detected toolchain is
    # this gcc (protoc and protobuf's editions defaults build from source), and aspect_rules_py
    # reads the host libc from `ldd --version`.
    pkgs.gcc
    pkgs.glibc.bin
  ];
  extraConfig.Entrypoint = [ "/bin/agentplane-runner" ];
}
