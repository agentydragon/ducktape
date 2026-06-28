# agent-box / codex user - home-manager config for the codex agent user.
#
# Deliberately slim (NOT ../home.nix): the codex identity is least-privilege, so
# it imports only the codex CLI plus the secrets it is actually granted
# (BuildBuddy, attic, the Forgejo bot key) — not agentydragon's full secret set.
#
# Bootstrap: NixOS sops-nix plants /home/codex/.ssh/id_ed25519 (agent-box-codex-user)
# before this activates; home-manager sops-nix below chains off that id.
{
  pkgs,
  config,
  ...
}:
{
  imports = [
    ../codex # OpenAI Codex CLI + config
    ../modules/sops-env.nix # ducktape.sopsEnv (BuildBuddy key reads this)
    ../modules/buildbuddy.nix # BuildBuddy creds -> bazelrc + BUILDBUDDY_API_KEY
    ../modules/forgejo-ssh.nix # Forgejo bot push key + git.allegedly.works ssh block
    ../modules/attic.nix # attic ~/.config/attic/config.toml (push/pull client)
  ];

  # home-manager sops-nix decrypts the codex user's secrets with its planted id.
  sops.age.sshKeyPaths = [ "${config.home.homeDirectory}/.ssh/id_ed25519" ];

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../secrets/hosts/agent-box-attic.yaml;
  };
  ducktape.forgejoSsh.sopsFile = ../../../ssh_keys/agent-box-codex-forgejo.sops.key;

  # This is a dedicated, scoped, isolated agent VM — let Codex run fully
  # unattended: execute every command without prompting and with no sandbox.
  ducktape.codex.settings = {
    approval_policy = "never";
    sandbox_mode = "danger-full-access";
  };

  programs.zsh.enable = true;
  programs.direnv = {
    enable = true;
    nix-direnv.enable = true;
  };
  programs.git = {
    enable = true;
    settings.user = {
      name = "codex";
      email = "codex@allegedly.works";
    };
  };

  home.packages = [ pkgs.psmisc ];

  home.username = "codex";
  home.homeDirectory = "/home/codex";
  home.stateVersion = "25.11";
}
