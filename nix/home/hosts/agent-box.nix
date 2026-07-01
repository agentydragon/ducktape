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
    ../modules/bazel.nix # User-level Bazel config and shared caches
    ../modules/sops-env.nix # ducktape.sopsEnv (BuildBuddy key reads this)
    ../modules/buildbuddy.nix # BuildBuddy creds -> bazelrc + BUILDBUDDY_API_KEY
    ../modules/forgejo-ssh.nix # Forgejo bot push key + git.allegedly.works ssh block
    ../modules/attic.nix # attic ~/.config/attic/config.toml (push/pull client)
    ../modules/agent-kubeconfig.nix # agent-box-codex k8s bearer kubeconfig
    ../modules/ssh.nix # Shared SSH client defaults
  ];

  # home-manager sops-nix decrypts the codex user's secrets with its planted id.
  sops.age.sshKeyPaths = [ "${config.home.homeDirectory}/.ssh/id_ed25519" ];

  ducktape.attic = {
    enable = true;
    sopsFile = ../../../secrets/hosts/agent-box-attic.yaml;
  };
  ducktape.forgejoSsh.sopsFile = ../../../ssh_keys/agent-box-codex-forgejo.sops.key;
  ducktape.ssh.enable = true;
  # TODO: this simple path still requires a home-manager activation after the
  # authentik-jwt-rotation CronJob commits a refreshed JWT. Replace with a local
  # token refresh/apply path if rotation staleness becomes operationally annoying.
  ducktape.agentKubeconfig = {
    enable = true;
    sopsFile = ../../../secrets/agent-box-codex-k8s-jwt.yaml;
    user = "agent-box-codex";
    namespace = "default";
  };

  # This is a dedicated, scoped, isolated agent VM — let Codex run fully
  # unattended: execute everything without prompting and with no sandbox. No
  # cluster/local models, no writable-roots list (localModels stays off, and
  # danger-full-access drops the sandbox block entirely).
  ducktape.codex = {
    approvalPolicy = "never";
    sandboxMode = "danger-full-access";
  };
  ducktape.bazel = {
    enable = true;
    userCache = {
      enable = true;
      diskCacheMaxSize = "80G";
    };
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

  home.packages = [
    pkgs.kubectl
    pkgs.psmisc
  ];

  home.username = "codex";
  home.homeDirectory = "/home/codex";
  home.stateVersion = "25.11";
}
