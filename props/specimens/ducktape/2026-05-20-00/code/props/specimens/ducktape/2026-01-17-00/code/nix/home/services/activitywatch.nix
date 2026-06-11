{
  config,
  lib,
  pkgs,
  enableGui,
  ...
}: {
  config = lib.mkIf enableGui {
    # Install ActivityWatch from nixpkgs
    home.packages = [pkgs.activitywatch];

    # SSH tunnel to VPS for ActivityWatch server connection
    systemd.user.services.aw-tunnel = {
      Unit = {
        Description = "ActivityWatch SSH tunnel to VPS";
        After = ["network-online.target"];
        Wants = ["network-online.target"];
      };
      Service = {
        ExecStart = "${pkgs.openssh}/bin/ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -L 5600:localhost:5600 agentydragon@agentydragon.com";
        Restart = "always";
        RestartSec = "5";
      };
      Install = {
        WantedBy = ["default.target"];
      };
    };

    # ActivityWatch configuration files
    xdg.configFile."activitywatch/aw-client/aw-client.toml".text = ''
      [server]
      # Same as default, except in our case this points to VPS to agentydragon.com
      # through a SSH tunnel, not a local HTTP server.
      hostname = "127.0.0.1"
      port = "5600"

      [client]
      #commit_interval = 10

      [server-testing]
      #hostname = "127.0.0.1"
      #port = "5666"

      [client-testing]
      #commit_interval = 5
    '';

    xdg.configFile."activitywatch/aw-qt/aw-qt.toml".text = ''
      [aw-qt]
      # Not starting aw-server -- data will go to agentydragon.com VPS
      autostart_modules = ["aw-watcher-afk", "aw-watcher-window"]# , "aw-notify"]

      [aw-qt-testing]
      #autostart_modules = ["aw-server", "aw-watcher-afk", "aw-watcher-window"]
    '';

    xdg.configFile."activitywatch/aw-watcher-afk/aw-watcher-afk.toml".text = ''
      [aw-watcher-afk]
      #timeout = 180
      #poll_time = 5

      [aw-watcher-afk-testing]
      #timeout = 20
      #poll_time = 1
    '';

    xdg.configFile."activitywatch/aw-watcher-window/aw-watcher-window.toml".text = ''
      [aw-watcher-window]
      #exclude_title = false
      #exclude_titles = []
      #poll_time = 1.0
      #strategy_macos = "swift"
    '';
  };
}
