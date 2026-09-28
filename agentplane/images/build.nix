# The build box's image (the `build` environment, cluster/cdk8s/agentplane/command_sandbox.py): the
# sandbox image (<sandbox.nix>) plus Bazel and what a build compiles with. Only this image carries
# them. A build can exhaust its container's memory, and that kills every process in the container:
# in the runner, the agent's harness with it.
#
# Build:  nix build .#agentplane-sandbox-build-image
# Load:   docker load < result
{ pkgs }:
import ./sandbox.nix {
  inherit pkgs;
  name = "agentplane-sandbox-build";
  extraPaths = [
    # `bazel` as bazelisk, which runs the upstream release a workspace's `.bazelversion` names: an
    # FHS binary, which the substrate lets run.
    (pkgs.runCommand "bazel-bazelisk" { } ''
      mkdir -p $out/bin
      ln -s ${pkgs.bazelisk}/bin/bazelisk $out/bin/bazel
    '')
    # What a local Bazel build compiles and probes with: rules_cc's auto-detected toolchain is this
    # gcc, whose wrapper carries binutils (protoc and protobuf's editions defaults build from
    # source), and aspect_rules_py reads the host libc from `ldd --version`.
    pkgs.gcc
    pkgs.glibc.bin
  ];
}
