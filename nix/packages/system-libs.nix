# System libraries needed for building and testing this repo.
#
# Shared between devShell (flake.nix), nix RBE image (x/nix_rbe_image/packages.nix),
# and mirrored by apt in the Ubuntu RBE image (devinfra/rbe_image/Dockerfile).
#
# Returns an attrset with:
#   buildInputs  — libraries with dev headers (compile-time + link-time)
#   packages     — runtime tools (dbus-daemon, xvfb-run)
#   libraryPath  — LD_LIBRARY_PATH string for prebuilt binaries
{ pkgs }:
let
  libs = [
    pkgs.openssl
    # Native dev headers for pip wheel builds (pygobject, pycairo, dbus-python)
    pkgs.gobject-introspection
    pkgs.cairo
    pkgs.dbus
    pkgs.ncurses5 # libtinfo5 for GHC toolchain
    # Chromium headless shell shared library dependencies (rules_playwright)
    pkgs.alsa-lib
    pkgs.at-spi2-atk
    pkgs.cups.lib
    pkgs.libdrm
    pkgs.mesa
    pkgs.nspr
    pkgs.nss
    pkgs.pango
    pkgs.libxcomposite
    pkgs.libxdamage
    pkgs.libxkbcommon
    pkgs.libxrandr
    pkgs.libxfixes
    pkgs.xorg.libxshmfence
  ];
in
{
  buildInputs = libs;
  packages = [
    pkgs.pkg-config
    (pkgs.lib.setPrio 20 pkgs.clang) # low priority to avoid shadowing gcc on RBE
    pkgs.dbus # dbus-daemon
    pkgs.xvfb-run # virtual framebuffer for headless GUI tests
  ];
  libraryPath = pkgs.lib.makeLibraryPath libs;
}
