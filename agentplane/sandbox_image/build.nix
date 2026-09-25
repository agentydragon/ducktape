# The build box's image (the `build` environment, cluster/cdk8s/agentplane/command_sandbox.py): the
# sandbox image (<default.nix>) plus `bazel`. Only this image carries it. A build can exhaust its
# container's memory, and that kills every process in the container: in the runner, the agent's
# harness with it.
#
# Build:  nix build .#agentplane-sandbox-build-image
# Load:   docker load < result
{ pkgs }:
import ./default.nix {
  inherit pkgs;
  name = "agentplane-sandbox-build";
  extraPaths = [
    # `bazel` as bazelisk, which runs the upstream release a workspace's `.bazelversion` names: an
    # FHS binary, which the substrate lets run.
    (pkgs.runCommand "bazel-bazelisk" { } ''
      mkdir -p $out/bin
      ln -s ${pkgs.bazelisk}/bin/bazelisk $out/bin/bazel
    '')
  ];
}
