# Attic binary cache credentials.
# Decrypts the per-host token from SOPS using ~/.ssh/id_ed25519 and uses it for
# both the Attic CLI and authenticated Nix substitution. Nix evaluation happens
# in the user's process, so it needs its own readable netrc even on NixOS hosts
# where the daemon has the same token in a root-only netrc.
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.attic;
in
{
  options.ducktape.attic = {
    enable = lib.mkEnableOption "Attic cache credentials";
    sopsFile = lib.mkOption {
      type = lib.types.path;
      description = "Path to the SOPS-encrypted YAML file containing the attic_token.";
    };
  };

  config = lib.mkIf cfg.enable {
    sops.secrets.attic_token = {
      inherit (cfg) sopsFile;
    };

    sops.templates."attic-config.toml" = {
      path = "${config.xdg.configHome}/attic/config.toml";
      content = ''
        default-server = "main"

        [servers.main]
        endpoint = "https://cache.allegedly.works"
        token = "${config.sops.placeholder.attic_token}"
      '';
      mode = "0600";
    };

    sops.templates."nix-attic-netrc" = {
      path = "${config.xdg.configHome}/nix/attic-netrc";
      content = ''
        machine cache.allegedly.works password ${config.sops.placeholder.attic_token}
      '';
      mode = "0600";
    };

    nix.settings = {
      experimental-features = [
        "nix-command"
        "flakes"
        "fetch-closure"
      ];
      substituters = [
        "https://cache.nixos.org/"
        "https://cache.allegedly.works/main?priority=40"
        "https://cache.allegedly.works/gaffer?priority=40"
      ];
      trusted-public-keys = [
        "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      ]
      ++ builtins.fromJSON (builtins.readFile ../../attic-pubkeys.json);
      netrc-file = config.sops.templates."nix-attic-netrc".path;
    };
  };
}
