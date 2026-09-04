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

  # Request-level accounting for the GitHub GraphQL drain investigated in
  # <debug/github_rate_limit_monitoring_blind_spot.md>. Host-scoped: wyrm2 is where
  # the burn is observed. Enabling only starts a loopback mitmdump and installs the
  # `claude-proxied` / `claude-desktop-proxied` wrappers -- the normal `claude` and
  # `claude-desktop` are untouched, so a stopped proxy cannot break them.
  ducktape.githubApiProxy.enable = true;

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../secrets/hosts/wyrm2-attic.yaml;
  };

  # Shared Bazel disk cache across local worktrees
  # (see ../modules/bazel-cache.nix). The 150G SSD holds both the cache/disk and
  # the per-worktree output bases, so cap the disk cache well below the default
  # 200G.
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
  ducktape.hakuApprovals.enable = true;
  ducktape.aiquota.remoteApi.enable = true;

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
  # scrolling at 4K on the gaming seat (debug/atlas/direct_display_bringup/README.md).
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
      echo "warning: Steam 'GPU accelerated rendering in web views' is not enabled (registry.vdf GPUAccelWebViewsV3 != 1). Big Picture will render on CPU (~5 FPS at 4K on the seat). Enable it in Steam → Settings → Interface. See debug/atlas/direct_display_bringup/README.md." >&2
    fi
  '';

  # Sway config for the game seat (seatphysical / 5090). The wrapped sway (with
  # --unsupported-gpu for NVIDIA) and its greeter session come from the NixOS
  # programs.sway module; package = null means "config only, reuse that sway".
  # foot/wofi are installed system-wide via programs.sway.extraPackages.
  wayland.windowManager.sway = {
    enable = true;
    package = null;
    # Force the initial workspace to 1. Otherwise sway names a fresh output's
    # first workspace after the first `workspace number N` binding with a free
    # name, and home-manager emits `Mod4+0 → number 10` before `Mod4+1 → number
    # 1` (attrset keys sort "Mod4+0" < "Mod4+1"), so boot lands on 10. A bare
    # `workspace` command in extraConfig (appended after the generated config)
    # runs at load and focuses 1.
    extraConfig = "workspace number 1";
    config = {
      modifier = "Mod4";
      terminal = "foot";
      menu = "wofi --show drun";
      # Replace the built-in swaybar with waybar (below): it carries the volume
      # applet and a larger font for the 4K panel.
      bars = [ ];
      startup = [
        { command = "waybar"; }
        # Process ~/.config/autostart (Signal/Telegram tray, etc.) — sway doesn't
        # run XDG autostart itself. Runs per sway session, so on both seats;
        # single-instance apps dedupe. TODO: scope to seat0 only if the game seat
        # shouldn't autostart chat apps.
        { command = "dex --autostart --environment sway"; }
        # Lock (not blank) on idle after 5 min. Deliberately NO `dpms off`:
        # blanking the DP output makes the FV43U KVM revert to USB-C on the
        # seatphysical seat (see notes below); swaylock keeps the output live.
        { command = "swayidle -w timeout 300 'swaylock -f'"; }
        # Login keyring is unlocked by pam_gnome_keyring on SDDM's PAM stack
        # (security.pam.services.sddm.enableGnomeKeyring in the wyrm2 host config),
        # so no keyring daemon needs starting here.
      ];
      # Keybindings mirror the user's GNOME + pop-shell muscle memory (dconf survey
      # in debug/atlas/direct_display_bringup/README.md). Focus stays on sway's default
      # Super+hjkl, which is exactly pop-shell focus-{left,down,up,right}.
      keybindings = lib.mkOptionDefault {
        "XF86AudioRaiseVolume" = "exec wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ 5%+";
        "XF86AudioLowerVolume" = "exec wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-";
        "XF86AudioMute" = "exec wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle";

        # Terminal on Super+Return (sway default), Super+t (GNOME media key), and
        # Ctrl+Alt+t (GNOME custom "Launch Terminal" binding).
        # TODO(terminal): foot is minimal and has no tabs. Consider adopting a
        # terminal with tabs/splits (kitty or wezterm) and update these bindings
        # + the sway default (Mod4+Return) to match.
        "Mod4+t" = "exec foot";
        "Control+Mod1+t" = "exec foot";
        # Lock on Super+Escape (GNOME screensaver binding). NOT Super+l — that is
        # pop-shell focus-right.
        "Mod4+Escape" = "exec swaylock -f";

        # Window management: pop-shell defaults + GNOME close/maximize.
        "Mod4+q" = "kill";
        "Mod4+g" = "floating toggle";
        "Mod4+o" = "layout toggle split";
        "Mod4+s" = "layout toggle stacking split";
        # GNOME toggle-maximized; fullscreen is the tiling analog.
        "Mod4+m" = "fullscreen";
        "Mod4+slash" = "exec wofi --show drun";
        # GNOME toggle-message-tray → open the swaync notification center.
        "Mod4+v" = "exec swaync-client -t -sw";

        # Horizontal workspaces on the arrows (GNOME switch-to-workspace-left/right),
        # overriding sway's default arrow-focus — focus lives on Super+hjkl.
        "Mod4+Left" = "workspace prev";
        "Mod4+Right" = "workspace next";
        "Mod4+Shift+Left" = "move container to workspace prev";
        "Mod4+Shift+Right" = "move container to workspace next";

        # Region screenshot to clipboard (Print, mirroring the GNOME flameshot key).
        "Print" = ''exec grim -g "$(slurp)" - | wl-copy'';
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
        "custom/swaync"
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
      # Notification-center toggle. `swaync-client -swb` feeds waybar JSON; the
      # label shows the state, trailing `*` = unread. Left-click opens the
      # center, right-click toggles do-not-disturb. Plain-text labels to match
      # the rest of the bar (no nerd-font dependency).
      "custom/swaync" = {
        tooltip = false;
        format = "{icon}";
        format-icons = {
          none = "Notif";
          notification = "Notif*";
          dnd-none = "DND";
          dnd-notification = "DND*";
          inhibited-none = "Notif";
          inhibited-notification = "Notif*";
          dnd-inhibited-none = "DND";
          dnd-inhibited-notification = "DND*";
        };
        return-type = "json";
        exec = "swaync-client -swb";
        on-click = "swaync-client -t -sw";
        on-click-right = "swaync-client -d -sw";
        escape = true;
      };
    };
    style = ''
      * {
        font-family: monospace;
        font-size: 15px;
      }
      #pulseaudio,
      #clock,
      #custom-swaync {
        padding: 0 10px;
      }
    '';
  };

  # Notification center for the sway seat(s): swaync gives popups + a slide-out
  # center panel (history + do-not-disturb), toggled from the waybar
  # custom/swaync module. Starts via the systemd user graphical-session
  # (wayland.windowManager.sway.systemd.enable is on by default); if it does not
  # come up, the sway session isn't reaching graphical-session.target — add
  # `exec swaync` to the sway `startup` list.
  #
  # swaync owns the per-user-bus `org.freedesktop.Notifications` singleton, so it
  # only functions once seat0 is off GNOME: GNOME's own daemon otherwise holds
  # that name on the shared /run/user/1001/bus (the same shared-user-bus limit
  # that keeps GNOME from running twice — see
  # debug/atlas/direct_display_bringup/README.md).
  services.swaync = {
    enable = true;
    settings = {
      positionX = "right";
      positionY = "top";
      control-center-width = 500;
      control-center-height = 600;
      notification-window-width = 420;
      timeout = 8;
      timeout-low = 4;
      timeout-critical = 0;
      fit-to-screen = true;
      keyboard-shortcuts = true;
      image-visibility = "when-available";
      widgets = [
        "title"
        "dnd"
        "notifications"
      ];
      widget-config = {
        title = {
          text = "Notifications";
          clear-all-button = true;
          button-text = "Clear all";
        };
        dnd.text = "Do not disturb";
      };
    };
  };

  home.packages = [
    # TODO: Add syncthing tray (syncthing-gtk not in nixpkgs).
    # Options: gnomeExtensions.syncthing-indicator, gnomeExtensions.syncthing-toggle, qsyncthingtray
    ducktapePackages.bebas-neue-font
    ducktapePackages.claude-desktop
    ducktapePackages.chatgpt
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
