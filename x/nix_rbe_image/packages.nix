# Shared package list for RBE worker images (both dockerTools and NixOS).
#
# These are the tools needed on BuildBuddy RBE workers and runner VMs.
# Both x/nix_rbe_image/default.nix (dockerTools) and
# x/nix_rbe_image/nixos.nix (NixOS container) import this list.
{ pkgs }:
with pkgs;
[
  # Shell + core utilities
  bash
  coreutils
  findutils
  gnugrep
  gnused
  gawk
  diffutils
  gnutar
  gzip
  xz
  bzip2
  which
  file
  patch
  less

  # Network tools
  curl
  wget
  cacert
  openssl

  # Build essentials
  gcc
  gnumake
  binutils
  patchelf
  cmake
  pkg-config

  # Crypto/TLS dev headers (Rust toolchain, pip wheel builds)
  openssl.dev

  # Clang: pip C extension builds (low priority to avoid conflicts with gcc)
  (pkgs.lib.setPrio 20 clang)

  # Java (BuildBuddy toolchain default)
  jdk11

  # Python
  python3

  # Bazel (bazelisk respects .bazelversion)
  bazelisk

  # SCM
  git

  # Docker CLI (daemon managed by BB's init-dockerd)
  # docker_29: docker_28 was marked insecure (unmaintained since 2025-11).
  docker_29

  # iptables for Firecracker Docker compatibility
  iptables

  # sudo
  sudo

  # Archive tools (Bazel needs zip/unzip)
  zip
  unzip

  # TODO: add back once image size is manageable
  # cpio             # Firecracker initramfs genrule
  # dbus             # private D-Bus sessions in tests
  # Chromium headless shell deps (rules_playwright):
  # alsa-lib at-spi2-atk cups libdrm mesa nspr nss pango
  # xorg.libXcomposite xorg.libXdamage libxkbcommon
  # xorg.libXrandr xorg.libXfixes xorg.libxshmfence
]
