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
    ../modules/forgejo-ssh.nix
    ../modules/no-screensaver.nix
    ../modules/kubeconfig.nix
    ../modules/talosconfig.nix
    ../modules/discord-minimized-autostart.nix
  ];

  ducktape.forgejoSsh.sopsFile = ../../../ssh_keys/wyrm2-forgejo.sops.key;

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../secrets/home/wyrm2/attic.yaml;
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

  # Place decrypted z.ai API key where aiquota reads it.
  # The Python CLI reads ~/.config/aiquota/config.toml for the key path.
  sops.secrets.zai_api_key_file = {
    sopsFile = ../../../secrets/home/wyrm2/zai.yaml;
    key = "zai_api_key";
  };

  xdg.configFile."aiquota/config.toml" = {
    text = ''
      [zai]
      api_key_path = "${config.sops.secrets.zai_api_key_file.path}"
    '';
  };

  # Steam defaults "GPU accelerated rendering in web views" OFF on NVIDIA +
  # Wayland (Valve driver-detection bug, ValveSoftware/steam-for-linux#13151).
  # That forces the Big Picture web UI (CEF) to rasterize on the CPU — ~5 FPS
  # scrolling at 4K on the gaming seat (debug/atlas/direct_display_bringup.md).
  # Enabling it (registry key GPUAccelWebViewsV3=1) renders on the 5090.
  #
  # Steam owns registry.vdf and rewrites it on exit but preserves keys once set,
  # so a best-effort re-assert on activation suffices. We only touch an existing
  # file (Steam creates it on first run) and only insert when the key is absent,
  # anchored on the gamescope refresh-rate key that Steam writes once the
  # gamescope session has run here. Caveat: enabling GPU web views on NVIDIA can
  # corrupt some Big Picture panels (#11843) — a separate Steam-side cosmetic bug.
  home.activation.steamGpuWebViews = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    reg="$HOME/.steam/registry.vdf"
    if [ -f "$reg" ] && ! grep -q GPUAccelWebViewsV3 "$reg"; then
      if ${pkgs.gawk}/bin/awk '
        { print }
        !ins && /"GamescopeEnableAppTargetRefreshRate2"/ {
          match($0, /^\t*/)
          printf "%s\"GPUAccelWebViewsV3\"\t\t\"1\"\n", substr($0, 1, RLENGTH)
          ins = 1
        }
      ' "$reg" > "$reg.hm-tmp"; then
        mv "$reg.hm-tmp" "$reg"
      else
        rm -f "$reg.hm-tmp"
      fi
    fi
  '';

  home.packages = [
    # TODO: Add syncthing tray (syncthing-gtk not in nixpkgs).
    # Options: gnomeExtensions.syncthing-indicator, gnomeExtensions.syncthing-toggle, qsyncthingtray
    ducktapePackages.aiquota
    ducktapePackages.bebas-neue-font
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
  # NixOS doesn't have Pop!_OS's built-in ubuntu-appindicators, so install it
  programs.gnome-shell.extensions = [
    { package = pkgs.gnomeExtensions.appindicator; }
    { package = ducktapePackages.aiquota; }
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
