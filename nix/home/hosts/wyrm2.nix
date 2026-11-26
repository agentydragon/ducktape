# Wyrm2 - NixOS dev workstation VM (Proxmox) + k8s worker
# Similar to rugged but for VM deployment.
{
  config,
  pkgs,
  lib,
  ducktapePackages,
  ...
}:
{
  imports = [
    ../home.nix
    ../modules/bazel-cache.nix
    ../modules/forgejo-ssh.nix
    ../modules/no-screensaver.nix
    ../modules/kubeconfig.nix
    ../modules/talosconfig.nix
    ../modules/discord-minimized-autostart.nix
    ../modules/karere.nix
    ./wyrm2-opencode.nix
  ];

  ducktape.forgejoSsh.sopsFile = ../../../ssh_keys/wyrm2-forgejo.sops.key;

  ducktape.codex.chatgptPackage = ducktapePackages.chatgpt;

  ducktape.sessionBackup = {
    enable = true;
    host = "wyrm2";
  };

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../secrets/hosts/wyrm2-attic.yaml;
  };

  # Shared Bazel caches across local worktrees (see ../modules/bazel-cache.nix).
  # The 300G SSD holds cache/disk, per-worktree output bases, and repository
  # archives and contents. Keep the action-cache cap below its 200G default to
  # leave room for those other caches; repo contents use age-only GC.
  ducktape.bazelCache = {
    enable = true;
    diskCacheGcMaxSize = "80G";
    repositoryCacheHardlinks = true;
    repoContentsCache = "${config.xdg.cacheHome}/bazel/_bazel_${config.home.username}/cache/repos/v1/contents";
  };

  ducktape.sopsEnv = {
    ANTHROPIC_API_KEY = {
      sopsFile = ../../../secrets/home/wyrm2/anthropic.yaml;
      key = "anthropic_api_key";
    };
    OPENAI_API_KEY = {
      sopsFile = ../../../secrets/home/wyrm2/openai.yaml;
      key = "openai_api_key";
    };
    GROQ_API_KEY = {
      sopsFile = ../../../secrets/home/wyrm2/groq.yaml;
      key = "groq_api_key";
    };
  };

  # ActivityWatch capture + import into the central server. Second device after
  # rugged, so wyrm2::<bucket> vs rugged::<bucket> exercises multi-device sync.
  # Importer-only over the bearer-gated write route; token from the shared Secret.
  ducktape.activitywatch.sync = {
    enable = true;
    dest = {
      url = "https://activitywatch-write.allegedly.works";
      device = "wyrm2";
      tokenSopsFile = ../../../cluster/k8s/activitywatch/activitywatch-write-token.sops.yaml;
    };
  };

  ducktape.aiquota.enable = true;
  ducktape.aiquota.remoteApi.enable = true;
  ducktape.plaidSpend.enable = true;

  # Keep GNOME's cursor selection declarative. Unmanaged dconf values can
  # otherwise override the installed theme with an invalid empty name/size.
  # TODO(cursor-dconf): Remove this override if the writer of those invalid
  # values is identified and fixed at the source.
  dconf.settings."org/gnome/desktop/interface" = {
    cursor-theme = "Adwaita";
    cursor-size = 24;
  };

  # Steam defaults "GPU accelerated rendering in web views" OFF on NVIDIA +
  # Wayland (Valve driver-detection bug, ValveSoftware/steam-for-linux#13151).
  # That forces the Big Picture web UI (CEF) to rasterize on the CPU — ~5 FPS
  # scrolling at 4K on the gaming seat (nix/debug/wyrm2/direct_display_bringup/README.md).
  # Enabling it (registry key GPUAccelWebViewsV3=1, via Steam → Settings →
  # Interface) renders on the 5090.
  #
  # Steam owns registry.vdf and rewrites it, so we do NOT edit it — this only
  # *warns* on activation if the key isn't enabled, leaving the fix to the
  # in-Steam toggle. (Enabling GPU web views on NVIDIA can also corrupt some
  # Big Picture panels, #11843 — a separate, cosmetic Steam-side bug.)
  home.activation.steamGpuWebViews = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    reg="$HOME/.steam/registry.vdf"
    if [ -f "$reg" ] && ! grep -qE '"GPUAccelWebViewsV3"[[:space:]]*"1"' "$reg"; then
      echo "warning: Steam 'GPU accelerated rendering in web views' is not enabled (registry.vdf GPUAccelWebViewsV3 != 1). Big Picture will render on CPU (~5 FPS at 4K on the seat). Enable it in Steam → Settings → Interface. See nix/debug/wyrm2/direct_display_bringup/README.md." >&2
    fi
  '';

  home.packages = [
    # TODO: Add syncthing tray (syncthing-gtk not in nixpkgs).
    # Options: gnomeExtensions.syncthing-indicator, gnomeExtensions.syncthing-toggle, qsyncthingtray
    pkgs.android-tools # adb/fastboot for Android device debugging
    ducktapePackages.bebas-neue-font
    ducktapePackages.claude-desktop
    pkgs.inkscape
    pkgs.kicad
    pkgs.openscad
    pkgs.psmisc
    # TODO: Add a GUI system monitor with graphs (psensor not in nixpkgs;
    # candidates: gnomeExtensions.vitals, gnomeExtensions.astra-monitor)
    pkgs.signal-desktop
    pkgs.telegram-desktop
    pkgs.tor-browser
    pkgs.tuxguitar
    ducktapePackages.tana-outliner
  ];

  # drivefs is provided by gaffer-private CI via cache.allegedly.works/gaffer
  # (per nix/gaffer-pins.json + nix/packages/gaffer.nix). Substituted, never
  # built from source on the consumer side.
  services.google-drive.enable = true;

  xdg.autostart = {
    enable = true;
    entries = [
      (pkgs.writeText "signal-desktop.desktop" ''
        [Desktop Entry]
        Type=Application
        Name=Signal
        Exec=signal-desktop --start-in-tray
        X-GNOME-Autostart-enabled=true
      '')
      (pkgs.writeText "telegram-desktop.desktop" ''
        [Desktop Entry]
        Type=Application
        Name=Telegram Desktop
        Exec=telegram-desktop -startintray
        X-GNOME-Autostart-enabled=true
      '')
    ];
  };

  home.stateVersion = "25.11";
}
