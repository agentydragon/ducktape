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
  ];

  ducktape.forgejoSsh.sopsFile = ../../../ssh_keys/wyrm2-forgejo.sops.key;

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../secrets/home/wyrm2/attic.yaml;
  };

  # Shared Bazel disk + repo-contents cache across local worktrees
  # (see ../modules/bazel-cache.nix). The 150G SSD holds both the cache/disk and
  # the per-worktree output bases, so cap the disk cache well below the default
  # 200G; repo-contents sharing is the bigger win here regardless.
  ducktape.bazelCache = {
    enable = true;
    diskCacheGcMaxSize = "80G";
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

  ducktape.activitywatch.sync = {
    enable = true;
    syncthing = {
      certFile = ../../../secrets/home/wyrm2/activitywatch-syncthing.cert.pem;
      keySopsFile = ../../../secrets/home/wyrm2/activitywatch-syncthing.sops.key;
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
      echo "warning: Steam 'GPU accelerated rendering in web views' is not enabled (registry.vdf GPUAccelWebViewsV3 != 1). Big Picture will render on CPU (~5 FPS at 4K on the seat). Enable it in Steam → Settings → Interface. See debug/atlas/direct_display_bringup.md." >&2
    fi
  '';

  # Sway config for the game seat (seat-game / 5090). The wrapped sway (with
  # --unsupported-gpu for NVIDIA) and its greeter session come from the NixOS
  # programs.sway module; package = null means "config only, reuse that sway".
  # foot/wofi are installed system-wide via programs.sway.extraPackages.
  wayland.windowManager.sway = {
    enable = true;
    package = null;
    config = {
      modifier = "Mod4";
      terminal = "foot";
      menu = "wofi --show drun";
      # Replace the built-in swaybar with waybar (below): it carries the volume
      # applet and a larger font for the 4K panel.
      bars = [ ];
      startup = [ { command = "waybar"; } ];
      keybindings = lib.mkOptionDefault {
        "XF86AudioRaiseVolume" = "exec wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ 5%+";
        "XF86AudioLowerVolume" = "exec wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-";
        "XF86AudioMute" = "exec wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle";
      };
    };
  };

  # Terminal font, ~40% over foot's 8pt default, for the 4K gaming-seat panel.
  programs.foot = {
    enable = true;
    settings.main.font = "monospace:size=11";
  };

  # Bottom bar for the sway seat: workspaces + clock + a clickable/scrollable
  # volume applet (scroll to adjust, click opens pavucontrol) + tray. Larger
  # font to match the 4K panel. Plain-text labels (no nerd-font glyphs) so it
  # renders without an icon-font dependency.
  programs.waybar = {
    enable = true;
    settings.mainBar = {
      layer = "top";
      position = "bottom";
      height = 30;
      modules-left = [
        "sway/workspaces"
        "sway/mode"
      ];
      modules-center = [ "clock" ];
      modules-right = [
        "pulseaudio"
        "tray"
      ];
      pulseaudio = {
        format = "Vol {volume}%";
        format-muted = "Vol muted";
        scroll-step = 5;
        # Cap scroll at 100% — HDMI sinks default to full scale and PipeWire
        # allows >100% (10000% = deafening), so never let the applet exceed it.
        max-volume = 100;
        on-click = "pavucontrol";
      };
      clock.format = "{:%a %d %b  %H:%M}";
      tray.spacing = 8;
    };
    style = ''
      * {
        font-family: monospace;
        font-size: 15px;
      }
      #pulseaudio,
      #clock {
        padding: 0 10px;
      }
    '';
  };

  home.packages = [
    # TODO: Add syncthing tray (syncthing-gtk not in nixpkgs).
    # Options: gnomeExtensions.syncthing-indicator, gnomeExtensions.syncthing-toggle, qsyncthingtray
    ducktapePackages.aiquota
    ducktapePackages.bebas-neue-font
    ducktapePackages.claude-desktop
    pkgs.inkscape
    pkgs.kicad
    pkgs.openscad
    pkgs.pavucontrol # GUI mixer opened by the waybar volume applet
    pkgs.psmisc
    # TODO: Add a GUI system monitor with graphs (psensor not in nixpkgs;
    # candidates: gnomeExtensions.vitals, gnomeExtensions.astra-monitor)
    pkgs.signal-desktop
    pkgs.telegram-desktop
    pkgs.tor-browser
    pkgs.tuxguitar
    ducktapePackages.tana-outliner
  ];
  programs.gnome-shell.extensions = [
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
