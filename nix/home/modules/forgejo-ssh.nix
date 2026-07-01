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

    programs.ssh.matchBlocks."git.allegedly.works" = {
      hostname = "git.allegedly.works";
      user = "git";
      port = 2222;
      identityFile = "~/.ssh/agentydragon_forgejo_id_ed25519";
      identitiesOnly = true;
    };
  };
}
