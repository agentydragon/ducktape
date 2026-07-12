{
  config,
  lib,
  pkgs,
  enableGui,
  ducktapePackages,
  ...
}:
let
  cfg = config.ducktape.activitywatch;
  toTOML = (pkgs.formats.toml { }).generate;
  syncRoot = cfg.sync.root;
  syncthingCfg = cfg.sync.syncthing;
  syncthingConfigured = syncthingCfg.certFile != null && syncthingCfg.keySopsFile != null;
  serverReadyScript = pkgs.writeShellScript "activitywatch-server-ready" ''
    set -eu

    for _ in $(${pkgs.coreutils}/bin/seq 1 30); do
      if ${pkgs.curl}/bin/curl -fsS http://127.0.0.1:${toString cfg.sync.localPort}/api/0/info >/dev/null; then
        exit 0
      fi
      ${pkgs.coreutils}/bin/sleep 1
    done

    echo "ActivityWatch server did not become ready on 127.0.0.1:${toString cfg.sync.localPort}" >&2
    exit 1
  '';
  syncPushScript = pkgs.writeShellScript "activitywatch-sync-push" ''
    exec ${pkgs.activitywatch}/bin/aw-sync \
      --host 127.0.0.1 \
      --port ${toString cfg.sync.localPort} \
      --sync-dir ${lib.escapeShellArg syncRoot} \
      sync-advanced \
      --mode push
  '';
in
{
  options.ducktape.activitywatch = {
    sync = {
      enable = lib.mkEnableOption "local ActivityWatch capture with aw-sync push into a Syncthing folder";

      root = lib.mkOption {
        type = lib.types.str;
        default = "${config.home.homeDirectory}/.activitywatch-sync";
        description = "Hidden root shared by aw-sync and Syncthing.";
      };

      interval = lib.mkOption {
        type = lib.types.str;
        default = "5min";
        description = "systemd timer interval for pushing local buckets into the sync folder.";
      };

      localPort = lib.mkOption {
        type = lib.types.port;
        default = 5600;
        description = "Local aw-server port used by watchers and aw-sync.";
      };

      syncthing = {
        certFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          description = "Public Syncthing certificate for this host's ActivityWatch device.";
        };

        keySopsFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          description = "SOPS binary-encrypted Syncthing private key for this host's ActivityWatch device.";
        };

        clusterDeviceName = lib.mkOption {
          type = lib.types.str;
          default = "activitywatch-cluster";
          description = "Syncthing device name for the cluster ActivityWatch receiver.";
        };

        clusterDeviceId = lib.mkOption {
          type = lib.types.str;
          default = "CXD63NS-6NVOEFY-AISQIJR-JOBNTDZ-3SCQPWP-K6PN3RN-KMHAIT4-RXYOBAR";
          description = "Syncthing device ID for the cluster ActivityWatch receiver.";
        };
      };
    };
  };

  config = lib.mkIf enableGui (
    lib.mkMerge [
      {
        # aw-server and aw-sync. The server runs headlessly under systemd;
        # awatcher owns window+AFK capture.
        home.packages = [ pkgs.activitywatch ];
      }

      (lib.mkIf cfg.sync.enable {
        # Window/AFK capture: awatcher (https://github.com/2e3s/awatcher) instead of
        # the stock aw-watcher-window/afk. The stock xlib watcher can't see windows on
        # GNOME/Wayland — Mutter doesn't maintain _NET_ACTIVE_WINDOW, and every other
        # out-of-process path is closed there (wlr-foreign-toplevel, Shell.Eval,
        # Introspect.GetWindows). awatcher reads focus via the focused-window-d-bus
        # GNOME Shell extension on GNOME, wlr-foreign-toplevel on wlroots, or xlib on
        # X11. It writes the same aw-watcher-window_<host>/aw-watcher-afk_<host>
        # buckets the stock watchers did, so aw-sync → Syncthing → cluster ingestion
        # is unchanged. See debug/activitywatch_window_gnome_wayland.md.
        home.packages = [ pkgs.awatcher ];

        # GNOME hosts need the in-shell extension that exports focus on the session
        # bus (awatcher polls it). Inert on wlroots/X11 hosts, where awatcher uses
        # the native protocol. Auto-added wherever gnome-shell is enabled.
        programs.gnome-shell.extensions = lib.optional config.programs.gnome-shell.enable {
          package = pkgs.gnomeExtensions.focused-window-d-bus;
        };

        systemd.user.services.awatcher = {
          Unit = {
            Description = "awatcher — ActivityWatch window/AFK watcher";
            Requires = [ "activitywatch-server.service" ];
            After = [
              "activitywatch-server.service"
              "graphical-session.target"
            ];
            PartOf = [ "graphical-session.target" ];
          };
          Service = {
            ExecStart = "${pkgs.awatcher}/bin/awatcher";
            # awatcher exits 0 (not a failure) when the focused-window-d-bus extension
            # isn't loaded yet (e.g. shell started before the install).
            # Restart=always retries until it is available; awatcher runs indefinitely
            # once wired.
            Restart = "always";
            RestartSec = 5;
          };
          Install = {
            WantedBy = [ "graphical-session.target" ];
          };
        };

        systemd.user.services.aw-watcher-tmux = {
          Unit = {
            Description = "ActivityWatch tmux session activity watcher";
            Requires = [ "activitywatch-server.service" ];
            After = [ "activitywatch-server.service" ];
          };
          Service = {
            ExecStart = lib.getExe ducktapePackages.aw-watcher-tmux;
            # Match tmux's user-runtime socket location (e.g.
            # /run/user/1001/tmux-1001/default) rather than its /tmp fallback.
            Environment = "TMUX_TMPDIR=%t";
            Restart = "on-failure";
            RestartSec = 5;
          };
          Install.WantedBy = [ "default.target" ];
        };

        assertions = [
          {
            assertion = (syncthingCfg.certFile == null) == (syncthingCfg.keySopsFile == null);
            message = "ducktape.activitywatch.sync.syncthing.certFile and keySopsFile must be set together.";
          }
        ];

        xdg.configFile."activitywatch/aw-client/aw-client.toml".source = toTOML "aw-client.toml" {
          server = {
            hostname = "127.0.0.1";
            port = toString cfg.sync.localPort;
          };
        };

        systemd.user.services.activitywatch-server = {
          Unit.Description = "ActivityWatch server";
          Service = {
            ExecStart = "${pkgs.activitywatch}/bin/aw-server --host 127.0.0.1 --port ${toString cfg.sync.localPort}";
            ExecStartPost = serverReadyScript;
            Restart = "on-failure";
            RestartSec = 5;
            TimeoutStartSec = 45;
          };
          Install.WantedBy = [ "default.target" ];
        };

        home.activation.activitywatchSyncDir = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
          mkdir -p '${syncRoot}'
        '';

        systemd.user.services.activitywatch-sync-push = {
          Unit = {
            Description = "Push ActivityWatch buckets into Syncthing sync folder";
            Requires = [ "activitywatch-server.service" ];
            After = [ "activitywatch-server.service" ];
          };
          Service = {
            Type = "oneshot";
            ExecStart = syncPushScript;
          };
        };

        systemd.user.timers.activitywatch-sync-push = {
          Unit.Description = "Push ActivityWatch buckets into Syncthing sync folder";
          Timer = {
            OnBootSec = "2min";
            OnUnitActiveSec = cfg.sync.interval;
            Unit = "activitywatch-sync-push.service";
          };
          Install.WantedBy = [ "timers.target" ];
        };
      })

      (lib.mkIf (cfg.sync.enable && syncthingConfigured) {
        sops.secrets.activitywatch_syncthing_key = {
          sopsFile = syncthingCfg.keySopsFile;
          format = "binary";
          mode = "0600";
        };

        services.syncthing = {
          enable = true;
          cert = toString syncthingCfg.certFile;
          key = config.sops.secrets.activitywatch_syncthing_key.path;
          # This is the ActivityWatch-only Syncthing topology. Keep its config
          # and database separate from any older general-purpose Syncthing
          # state in ~/.config/syncthing; the Home Manager module also uses this
          # path when it reconciles the managed folders and devices.
          extraOptions = [ "--home=${config.xdg.stateHome}/syncthing" ];
          overrideDevices = true;
          overrideFolders = true;
          settings = {
            devices.${syncthingCfg.clusterDeviceName} = {
              id = syncthingCfg.clusterDeviceId;
              name = syncthingCfg.clusterDeviceName;
            };
            folders.${syncRoot} = {
              id = "activitywatch";
              label = "ActivityWatch";
              path = syncRoot;
              type = "sendonly";
              devices = [ syncthingCfg.clusterDeviceName ];
              rescanIntervalS = 60;
              fsWatcherEnabled = true;
            };
            options = {
              relaysEnabled = true;
              urAccepted = -1;
            };
          };
        };
      })
    ]
  );
}
