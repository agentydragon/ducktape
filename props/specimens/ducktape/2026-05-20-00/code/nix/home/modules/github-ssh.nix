# GitHub SSH key for git@github.com.
# Decrypted from SOPS at home-manager activation time using ~/.ssh/id_ed25519.
{
  config,
  lib,
  ...
}:
let
  cfg = config.ducktape.githubSsh;
in
{
  options.ducktape.githubSsh.sopsFile = lib.mkOption {
    type = lib.types.path;
    description = "Path to the SOPS binary-encrypted GitHub SSH private key.";
  };

  config = {
    sops.secrets.github_ssh_key = {
      inherit (cfg) sopsFile;
      format = "binary";
      path = "${config.home.homeDirectory}/.ssh/agentydragon_github_id_ed25519";
      mode = "0600";
    };

    programs.ssh.matchBlocks."github.com" = {
      hostname = "github.com";
      user = "git";
      identityFile = "~/.ssh/agentydragon_github_id_ed25519";
      identitiesOnly = true;
    };
  };
}
