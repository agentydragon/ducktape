{
  config,
  lib,
  pkgs,
  enableGui,
  ...
}:
let
  cfg = config.ducktape.activitywatch;
  toTOML = (pkgs.formats.toml { }).generate;
  syncRoot = cfg.sync.root;
  syncthingCfg = cfg.sync.syncthing;
  syncthingConfigured = syncthingCfg.certFile != null && syncthingCfg.keySopsFile != null;
  syncPushScript = pkgs.writeShellScript "activitywatch-sync-push" ''
    set -eu

    mkdir -p ${lib.escapeShellArg syncRoot}
    for _ in $(${pkgs.coreutils}/bin/seq 1 30); do
      if ${pkgs.curl}/bin/curl -fsS http://127.0.0.1:${toString cfg.sync.localPort}/api/0/info >/dev/null; then
        exec ${pkgs.activitywatch}/bin/aw-sync \
          --host 127.0.0.1 \
          --port ${toString cfg.sync.localPort} \
          --sync-dir ${lib.escapeShellArg syncRoot} \
          sync-advanced \
          --mode push
      fi
      ${pkgs.coreutils}/bin/sleep 1
    done

    echo "ActivityWatch local server is not reachable on 127.0.0.1:${toString cfg.sync.localPort}; skipping sync push"
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
        # Install ActivityWatch from nixpkgs.
        home.packages = [ pkgs.activitywatch ];

        xdg.configFile."activitywatch/aw-watcher-afk/aw-watcher-afk.toml".source =
          toTOML "aw-watcher-afk.toml"
            {
              # Available options: timeout (default 180), poll_time (default 5)
              aw-watcher-afk = { };
              # Available options: timeout (default 20), poll_time (default 1)
              aw-watcher-afk-testing = { };
            };

        xdg.configFile."activitywatch/aw-watcher-window/aw-watcher-window.toml".source =
          toTOML "aw-watcher-window.toml"
            {
              # Available options: poll_time (default 1.0), exclude_title (default false),
              # exclude_titles (default [])
              aw-watcher-window = { };
            };
      }

      (lib.mkIf cfg.sync.enable {
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

        xdg.configFile."activitywatch/aw-qt/aw-qt.toml".source = toTOML "aw-qt.toml" {
          aw-qt.autostart_modules = [
            "aw-server"
            "aw-watcher-afk"
            "aw-watcher-window"
          ];
        };

        home.activation.activitywatchSyncDir = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
          mkdir -p '${syncRoot}'
        '';

        systemd.user.services.activitywatch-sync-push = {
          Unit.Description = "Push ActivityWatch buckets into Syncthing sync folder";
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
