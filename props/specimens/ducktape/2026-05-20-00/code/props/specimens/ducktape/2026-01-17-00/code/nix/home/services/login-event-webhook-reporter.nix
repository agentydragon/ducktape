{
  config,
  lib,
  pkgs,
  enableGui,
  ...
}: let
  # Python script with dependencies
  login-event-webhook-reporter = pkgs.python3.pkgs.buildPythonApplication {
    pname = "login-event-webhook-reporter";
    version = "1.0.0";
    format = "other";

    src = pkgs.writeTextFile {
      name = "login_event_webhook_reporter.py";
      text = builtins.readFile ./login_event_webhook_reporter.py;
      executable = true;
    };

    propagatedBuildInputs = with pkgs.python3.pkgs; [
      pydbus
      pygobject3
      systemd-python
    ];

    dontUnpack = true;
    dontBuild = true;

    installPhase = ''
      mkdir -p $out/bin
      cp $src $out/bin/login_event_webhook_reporter
      chmod +x $out/bin/login_event_webhook_reporter
    '';

    meta = {
      description = "Reports GNOME session events (login, lock, suspend) to webhook endpoint";
      platforms = pkgs.lib.platforms.linux;
    };
  };
in {
  config = lib.mkIf enableGui {
    home.packages = [login-event-webhook-reporter];

    systemd.user.services.login_event_webhook_reporter = {
      Unit = {
        Description = "Login Event Webhook Reporter";
        After = ["graphical-session.target"];
        Wants = ["graphical-session.target"];
      };
      Service = {
        ExecStart = "${login-event-webhook-reporter}/bin/login_event_webhook_reporter";
        Restart = "on-failure";
      };
      Install = {
        WantedBy = ["graphical-session.target"];
      };
    };
  };
}
