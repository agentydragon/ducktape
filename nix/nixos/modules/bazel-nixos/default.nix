# Install NixOS-specific Bazel flags to /etc/bazel.bazelrc.
# System-level bazelrc is read regardless of $HOME, so it works even when
# Claude Code's sandbox overrides HOME.
_: {
  environment.etc."bazel.bazelrc".source = ./system.bazelrc;
}
