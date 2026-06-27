# GPU crash mitigation + diagnostics for the Lunar Lake (Arc 130V/140V) iGPU.
#
# gnome-shell aborts every few days inside Mesa iris's batch-submit error path
# (_iris_batch_flush -> abort), which on Wayland tears the whole session down to
# GDM (tmux survives — the machine never reboots). Full forensics, including the
# native backtraces shared across crashes, are in:
#   debug/rugged/gnome_shell_iris_abort.md
#
# This module does two things:
#   1. Mitigation — bump Mesa from the stable channel's 25.2.6 to nixpkgs-unstable
#      (26.1.x), pulling in ~8 months of Intel/LNL driver work. No release note
#      names this exact abort, so it is an attempt, not a guaranteed fix.
#   2. Diagnostics — make the *next* crash conclusive: log the iris submit errno,
#      retain gnome-shell cores, and capture kernel xe devcoredumps before they
#      auto-expire.
{
  config,
  inputs,
  lib,
  pkgs,
  ...
}:
let
  # nixpkgs-unstable mesa is 26.1.2 and Hydra-cached. nixpkgs-master has 26.1.3
  # but is not built by the binary cache, so it would compile Mesa from source.
  pkgsUnstable = import inputs.nixpkgs-unstable {
    inherit (pkgs.stdenv.hostPlatform) system;
    config.allowUnfree = true;
  };
in
{
  # --- Mitigation: newer Mesa --------------------------------------------------
  # Swap only the graphics driver package; the rest of the system stays on the
  # stable channel. hardware.graphics.package feeds /run/opengl-driver, which is
  # where gnome-shell loads its iris / EGL / Vulkan drivers from.
  hardware.graphics = {
    package = pkgsUnstable.mesa;
    package32 = pkgsUnstable.pkgsi686Linux.mesa;
  };

  # --- Diagnostics for the next crash -----------------------------------------

  # iris logs the GPU submit failure (and errno) to stderr — which lands in the
  # journal under org.gnome.Shell@wayland.service — before it calls abort().
  # CLEANUP: remove MESA_DEBUG once the iris batch-flush abort is root-caused
  #   (debug/rugged/gnome_shell_iris_abort.md). It is global session log noise.
  environment.sessionVariables.MESA_DEBUG = "1";

  # Keep gnome-shell cores long enough to inspect (default rotates aggressively).
  systemd.coredump.extraConfig = ''
    MaxUse=4G
    KeepFree=2G
    ProcessSizeMax=8G
    ExternalSizeMax=8G
  '';

  # The xe kernel driver drops a devcoredump under /sys/class/devcoredump/ on a
  # GPU fault, but it self-deletes after ~5 minutes. On appearance, copy it to
  # /var/lib/devcoredump (which also frees the kernel slot).
  services.udev.extraRules = ''
    ACTION=="add", SUBSYSTEM=="devcoredump", TAG+="systemd", ENV{SYSTEMD_WANTS}+="capture-devcoredump@$kernel.service"
  '';
  systemd.services."capture-devcoredump@" = {
    description = "Capture GPU devcoredump %i";
    path = [ pkgs.coreutils ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart =
        let
          script = pkgs.writeShellScript "capture-devcoredump" ''
            set -eu
            dev="/sys/class/devcoredump/$1"
            out="/var/lib/devcoredump"
            mkdir -p "$out"
            ts="$(date +%Y%m%d-%H%M%S)"
            [ -r "$dev/data" ] && cp "$dev/data" "$out/$ts-$1.bin" || true
            # Writing to data releases the kernel devcoredump slot.
            echo 1 > "$dev/data" 2>/dev/null || true
          '';
        in
        "${script} %i";
    };
  };
}
