# Shared home-manager base for agent-box agent users (codex, zai).
#
# Deliberately slim (NOT ../../home.nix): each agent identity is least-privilege, so
# this imports only the secrets it is granted (BuildBuddy, attic, the Forgejo bot key,
# an agent k8s kubeconfig) plus zsh/direnv/git — not agentydragon's full secret set.
# Per-user files (codex.nix, zai.nix) import this with their params and add the
# agent-specific CLI (codex / claude-code).
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
  kubeconfigUser,
  forgejoKeySopsFile,
  forgejoTeaSopsFile,
  kubeJwtSopsFile,
}:
{
  pkgs,
  config,
  ...
}:
{
  imports = [
    ../../modules/neovim.nix
    ../../modules/tmux.nix
    ../../modules/sops-env.nix # ducktape.sopsEnv
    ../../modules/buildbuddy.nix # BuildBuddy creds -> bazelrc + BUILDBUDDY_API_KEY
    ../../modules/forgejo-ssh.nix # Forgejo bot push key + git.allegedly.works ssh block
    ../../modules/forgejo-tea.nix # Forgejo API token -> ~/.config/tea/config.yml
    ../../modules/attic.nix # attic ~/.config/attic/config.toml (push/pull client)
    ../../modules/agent-kubeconfig.nix # agent-box-<user> k8s bearer kubeconfig
  ];

  # home-manager sops-nix decrypts this user's secrets with its planted id.
  sops.age.sshKeyPaths = [ "${config.home.homeDirectory}/.ssh/id_ed25519" ];

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../../secrets/hosts/agent-box-attic.yaml;
  };
  ducktape.forgejoSsh.sopsFile = forgejoKeySopsFile;
  # TODO: like the kubeconfig JWT path below, this relies on a home-manager
  # activation after forgejo-token-rotation commits a refreshed SOPS file.
  ducktape.forgejoTea = {
    enable = true;
    sopsFile = forgejoTeaSopsFile;
  };
  # TODO: this simple path still requires a home-manager activation after the
  # authentik-jwt-rotation CronJob commits a refreshed JWT. Replace with a local
  # token refresh/apply path if rotation staleness becomes operationally annoying.
  ducktape.agentKubeconfig = {
    enable = true;
    sopsFile = kubeJwtSopsFile;
    user = kubeconfigUser;
    namespace = "default";
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
    pkgs.kubectl
    pkgs.psmisc
  ];

  home.username = username;
  home.homeDirectory = homeDirectory;
  home.stateVersion = "25.11";
}
