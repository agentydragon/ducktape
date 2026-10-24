# Shared home-manager base for the agent-box Codex user.
#
# Deliberately slim (NOT ../../home.nix): this imports only the secrets it is
# granted (BuildBuddy and attic) plus zsh/direnv/git — not agentydragon's full
# secret set.
# The Codex module imports this with its params and adds the agent-specific CLI.
#
# This is a function from per-user params to a home-manager module, so the per-user
# files call `import ./common.nix { ... }` inside their own `imports`.
#
# Bootstrap: NixOS sops-nix plants /home/<user>/.ssh/id_ed25519
# (agent-box-<user>-user) before this activates; the sops-nix block below chains this
# user's secrets off that id.
{
  username,
  homeDirectory,
  gitName,
  gitEmail,
}:
{
  pkgs,
  config,
  ...
}:
{
  imports = [
    ../../../../../nix/home/modules/neovim.nix
    ../../../../../nix/home/modules/tmux.nix
    ../../../../../nix/home/modules/sops-env.nix # ducktape.sopsEnv
    ../../../../../nix/home/modules/buildbuddy.nix # BuildBuddy creds -> bazelrc/API key
    ../../../../../nix/home/modules/attic.nix # Attic push/pull client config
  ];

  # home-manager sops-nix decrypts this user's secrets with its planted id.
  sops.age.sshKeyPaths = [ "${config.home.homeDirectory}/.ssh/id_ed25519" ];

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../../../secrets/hosts/agent-box-attic.yaml;
  };
  programs.zsh.enable = true;
  programs.direnv = {
    enable = true;
    nix-direnv.enable = true;
  };
  programs.git = {
    enable = true;
    settings.user = {
      name = gitName;
      email = gitEmail;
    };
  };

  home.packages = [
    pkgs.psmisc
  ];

  home.username = username;
  home.homeDirectory = homeDirectory;
  home.stateVersion = "25.11";
}
