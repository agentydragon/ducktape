# Bazel configuration for Pop!_OS hosts running Nix home-manager
#
# On Pop!_OS + home-manager, Nix's gcc wrapper is in PATH and gets auto-detected
# as the CC toolchain by Bazel. This produces proc-macro .so files linked against
# Nix's glibc (2.40), but Bazel's downloaded rustc uses the system ld-linux which
# only has glibc 2.35. This causes "GLIBC_2.39 not found" when rustc tries to
# dlopen proc-macros at compile time.
#
# Fix: Force system CC toolchain for Rust builds.
{
  config,
  lib,
  ...
}: {
  # Force system CC toolchain for Rust proc-macro compatibility
  home.file.".bazelrc".text = lib.mkAfter ''
    build --action_env=CC=/usr/bin/gcc
    build --action_env=CXX=/usr/bin/g++
    build --action_env=OPENSSL_DIR=/usr
    build --action_env=OPENSSL_LIB_DIR=/usr/lib/x86_64-linux-gnu
  '';
}
