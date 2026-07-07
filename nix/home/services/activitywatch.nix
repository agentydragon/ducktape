{
  config,
  lib,
  pkgs,
  enableGui,
  osConfig ? null,
  ...
}:
let
  cfg = config.ducktape.activitywatch;
  toTOML = (pkgs.formats.toml { }).generate;
  syncRoot = cfg.sync.root;
  hostSyncDir = "${syncRoot}/${cfg.sync.hostname}";
  startDateArgs = lib.optionalString (
    cfg.sync.startDate != null
  ) "--start-date ${lib.escapeShellArg cfg.sync.startDate}";
  syncPushScript = pkgs.writeShellScript "activitywatch-sync-push" ''
    set -eu

    mkdir -p ${lib.escapeShellArg hostSyncDir}
    for _ in $(${pkgs.coreutils}/bin/seq 1 30); do
      if ${pkgs.curl}/bin/curl -fsS http://127.0.0.1:${toString cfg.sync.localPort}/api/0/info >/dev/null; then
        exec ${pkgs.activitywatch}/bin/aw-sync \
          --host 127.0.0.1 \
          --port ${toString cfg.sync.localPort} \
          --sync-dir ${lib.escapeShellArg hostSyncDir} \
          sync-advanced \
          --mode push \
          ${startDateArgs}
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

      hostname = lib.mkOption {
        type = lib.types.str;
        default =
          if osConfig != null && osConfig ? networking && osConfig.networking ? hostName then
            osConfig.networking.hostName
          else
            config.home.username;
        description = "Host label used for the ActivityWatch sync directory and synced bucket provenance.";
      };

      root = lib.mkOption {
        type = lib.types.str;
        default = "${config.home.homeDirectory}/.activitywatch-sync";
        description = "Hidden root shared by aw-sync and Syncthing.";
      };

      startDate = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "2026-07-06";
        description = "Optional YYYY-MM-DD lower bound passed to aw-sync.";
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

      (lib.mkIf (!cfg.sync.enable) {
        # Server runs in the K8s cluster with a Nebula sidecar (cert name
        # "activitywatch"). Lighthouse DNS resolves the bare name to the pod's
        # Nebula IP.
        xdg.configFile."activitywatch/aw-client/aw-client.toml".source = toTOML "aw-client.toml" {
          server = {
            hostname = "activitywatch";
            port = "5600";
          };
        };

        xdg.configFile."activitywatch/aw-qt/aw-qt.toml".source = toTOML "aw-qt.toml" {
          # No local server; data goes to the cluster via Nebula mesh.
          aw-qt.autostart_modules = [
            "aw-watcher-afk"
            "aw-watcher-window"
          ];
        };
      })

      (lib.mkIf cfg.sync.enable {
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
          mkdir -p '${hostSyncDir}'
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
    ]
  );
}
