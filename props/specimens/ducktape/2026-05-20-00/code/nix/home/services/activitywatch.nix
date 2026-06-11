{
  config,
  lib,
  pkgs,
  enableGui,
  ...
}:
let
  toTOML = (pkgs.formats.toml { }).generate;
in
{
  config = lib.mkIf enableGui {
    # Install ActivityWatch from nixpkgs
    home.packages = [ pkgs.activitywatch ];

    # ActivityWatch configuration files
    # Server runs in the K8s cluster with a Nebula sidecar (cert name "activitywatch").
    # Lighthouse DNS resolves the bare name to the pod's Nebula IP.
    xdg.configFile."activitywatch/aw-client/aw-client.toml".source = toTOML "aw-client.toml" {
      server = {
        hostname = "activitywatch";
        port = "5600";
      };
    };

    xdg.configFile."activitywatch/aw-qt/aw-qt.toml".source = toTOML "aw-qt.toml" {
      # No local server — data goes to the cluster via Nebula mesh.
      aw-qt.autostart_modules = [
        "aw-watcher-afk"
        "aw-watcher-window"
      ];
    };

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
  };
}
