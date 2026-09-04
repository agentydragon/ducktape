# Claude Desktop (GUI app) — Anthropic's Electron desktop client for Linux.
# Installed from the official .deb in Anthropic's apt repo (NOT Claude Code the
# CLI — that's a separate `programs.claude-code` module). The app ships its own
# Chromium/Electron runtime; we only supply the system shared libs it links
# against and patch the ELF RPATHs via autoPatchelfHook.
#
# To update (the apt repo is the source of truth, not a GitHub release):
#   curl -sSL https://downloads.claude.ai/claude-desktop/apt/stable/dists/stable/main/binary-amd64/Packages \
#     | awk 'BEGIN{RS=""} /Version:/{print}' | tail -1
#   # then bump `version`, re-fetch the .deb, and recompute the SRI hash:
#   nix hash to-sri --type sha256 "$(curl -sSL <deb-url> | sha256sum | cut -d' ' -f1)"
# Install docs: https://code.claude.com/docs/en/desktop-linux
{
  lib,
  pkgs,
}:
let
  version = "1.40609.1";
in
pkgs.stdenv.mkDerivation {
  pname = "claude-desktop";
  inherit version;

  src = pkgs.fetchurl {
    url = "https://downloads.claude.ai/claude-desktop/apt/stable/pool/main/c/claude-desktop/claude-desktop_${version}_amd64.deb";
    hash = "sha256-gBguhRHGu+5t4mx+4iX70qmroidO8UBaHYnNj+ejgNw=";
  };

  nativeBuildInputs = with pkgs; [
    autoPatchelfHook
    makeWrapper
  ];

  # Maps the .deb's Depends (libgtk-3-0, libnss3, libatspi2.0-0, libdrm2,
  # libgbm1, libsecret-1-0, libxtst6, ...). autoPatchelf patches DT_NEEDED;
  # the GL/X libs are also dlopen'd by Electron, so makeWrapper exposes them on
  # LD_LIBRARY_PATH too (see tana-outliner.nix for the same shape).
  #
  # libsecret is the non-obvious one: Electron's safeStorage/OSCrypt dlopens
  # libsecret-1.so.0 (it is NOT a DT_NEEDED), so without it on LD_LIBRARY_PATH
  # the dlopen fails and safeStorage.isEncryptionAvailable() returns false —
  # Claude Desktop then refuses to persist sign-in with a misleading "install a
  # system keyring" prompt even though gnome-keyring is running and unlocked.
  buildInputs = with pkgs; [
    alsa-lib
    at-spi2-atk
    at-spi2-core
    cairo
    cups
    dbus
    expat
    glib
    gtk3
    libdrm
    libGL
    libnotify
    libsecret
    libxkbcommon
    mesa
    # virtiofsd (bundled for the Cowork microVM feature) needs these.
    libseccomp
    libcap_ng
    nspr
    nss
    pango
    util-linux # libuuid
    libx11
    libxcomposite
    libxdamage
    libxext
    libxfixes
    libxrandr
    libxtst
    libxcb
  ];

  runtimeDependencies = [ (lib.getLib pkgs.systemd) ];

  dontBuild = true;
  dontConfigure = true;
  dontUnpack = true;

  installPhase = ''
    runHook preInstall

    # Extract .deb via bsdtar (tolerates the SUID chrome-sandbox without error).
    ${pkgs.libarchive}/bin/bsdtar -xf $src --no-same-permissions
    ${pkgs.libarchive}/bin/bsdtar -xf data.tar.* --no-same-permissions

    mkdir -p $out/lib
    cp -r usr/share $out/share
    cp -r usr/lib/claude-desktop $out/lib/claude-desktop

    # Wrap the real binary so dlopen'd GL/X libs — and libsecret (dlopen'd by
    # Electron's OSCrypt for safeStorage; see buildInputs note above) — resolve.
    # qemu_kvm goes on PATH so the "Cowork" sandboxed-microVM feature's presence
    # check finds qemu-system-x86_64 (firmware + virtiofsd are handled at the
    # system level by the ducktape.cowork NixOS module).
    mkdir -p $out/bin
    makeWrapper $out/lib/claude-desktop/claude-desktop $out/bin/claude-desktop \
      --prefix LD_LIBRARY_PATH : "${
        lib.makeLibraryPath (
          with pkgs;
          [
            libGL
            libdrm
            libsecret
            libxkbcommon
            mesa
            libx11
            libxtst
            libxcb
          ]
        )
      }" \
      --prefix PATH : "${lib.makeBinPath [ pkgs.qemu_kvm ]}"

    # Point the desktop entry's Exec lines at our wrapper. Match the three
    # `Exec=claude-desktop <arg>` lines (main + NewChat/NewCode actions) without
    # touching StartupWMClass/Icon, which also contain the literal name.
    #
    # Gotcha: the entry is named `com.anthropic.Claude.desktop`, not after the
    # binary. It was `claude-desktop.desktop` up to 1.18286.0, so a version bump
    # can rename it out from under `--replace-fail` and fail the install phase;
    # the icons kept the old `claude-desktop` stem.
    substituteInPlace $out/share/applications/com.anthropic.Claude.desktop \
      --replace-fail "Exec=claude-desktop " "Exec=$out/bin/claude-desktop "

    runHook postInstall
  '';

  meta = {
    description = "Claude Desktop — Anthropic's Electron desktop app (Linux beta)";
    homepage = "https://code.claude.com/docs/en/desktop-linux";
    license = lib.licenses.unfree;
    platforms = [ "x86_64-linux" ];
    mainProgram = "claude-desktop";
  };
}
