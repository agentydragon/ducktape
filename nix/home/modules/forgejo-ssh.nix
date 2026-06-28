# Forgejo SSH key for git@git.allegedly.works.
# Decrypted from SOPS at home-manager activation time using ~/.ssh/id_ed25519.
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.forgejoSsh;
in
{
  options.ducktape.forgejoSsh.sopsFile = lib.mkOption {
    type = lib.types.path;
    description = "Path to the SOPS binary-encrypted Forgejo SSH private key.";
  };

  config = {
    sops.secrets.forgejo_ssh_key = {
      inherit (cfg) sopsFile;
      format = "binary";
      path = "${config.home.homeDirectory}/.ssh/agentydragon_forgejo_id_ed25519";
      mode = "0600";
    };

    # home-manager only writes ~/.ssh/config (and thus the matchBlock below) when
    # programs.ssh is enabled. Full home.nix hosts set this explicitly; the slim
    # agent-box codex config doesn't — so default it on here, or this module's
    # matchBlock is silently dropped and git falls back to port 22 + the default
    # key (Permission denied). mkDefault lets home.nix's explicit value still win.
    programs.ssh.enable = lib.mkDefault true;

    programs.ssh.matchBlocks."git.allegedly.works" = {
      hostname = "git.allegedly.works";
      user = "git";
      port = 2222;
      identityFile = "~/.ssh/agentydragon_forgejo_id_ed25519";
      identitiesOnly = true;
    };
  };
}
