{
  config,
  lib,
  ducktapePackages,
  ...
}:
let
  cfg = config.ducktape.plaidSpend;
  package = ducktapePackages.plaidSpendDesktop;
in
{
  options.ducktape.plaidSpend = {
    enable = lib.mkEnableOption "Plaid statement-cycle spend panel client";

    apiUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://plaid-spend.allegedly.works";
      description = "HTTPS base URL of the Plaid Spend API.";
    };

    oidcIssuer = lib.mkOption {
      type = lib.types.str;
      default = "https://auth.allegedly.works/application/o/plaid-spend-desktop/";
      description = "Authentik OIDC issuer used for Plaid Spend sign-in.";
    };

    oidcClientId = lib.mkOption {
      type = lib.types.str;
      default = "plaid-spend-desktop";
      description = "Public Authentik OIDC client ID registered for the desktop PKCE flow.";
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ package ];
    programs.gnome-shell.extensions = [ { inherit package; } ];

    systemd.user.services.plaid-spend = {
      Unit = {
        Description = "Plaid Spend desktop client";
        After = [ "graphical-session.target" ];
        PartOf = [ "graphical-session.target" ];
      };
      Service = {
        Type = "simple";
        ExecStart = "${package}/bin/plaid-spend-daemon";
        Environment = [
          "PLAID_SPEND_API_URL=${cfg.apiUrl}"
          "PLAID_SPEND_OIDC_ISSUER=${cfg.oidcIssuer}"
          "PLAID_SPEND_OIDC_CLIENT_ID=${cfg.oidcClientId}"
          "PYTHONUNBUFFERED=1"
        ];
        UMask = "0077";
        NoNewPrivileges = true;
        Restart = "on-failure";
        RestartSec = 5;
      };
      Install.WantedBy = [ "graphical-session.target" ];
    };
  };
}
