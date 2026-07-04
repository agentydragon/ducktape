# home-manager config for the codex pod. Baked into the image at build time
# (its home-files are copied into /home/codex), so the pod needs no runtime
# bootstrap script for static config. Secrets are NOT here — they come from k8s
# (BUILDBUDDY_API_KEY env, the id_ed25519 plant, ESO-templated files); so no
# sops-nix, no systemd, non-root.
_: {
  home.username = "codex";
  home.homeDirectory = "/home/codex";
  home.stateVersion = "25.11";

  programs.home-manager.enable = true;
  targets.genericLinux.enable = true;

  programs.bash.enable = true;
  programs.git = {
    enable = true;
    settings.user = {
      name = "codex-pod";
      email = "codex-pod@allegedly.works";
    };
  };

  # Forgejo push over SSH (AGit). Only the key is a secret — it's planted at
  # runtime from a k8s Secret; this static matchBlock is baked here.
  programs.ssh = {
    enable = true;
    matchBlocks."git.allegedly.works" = {
      hostname = "git.allegedly.works";
      user = "git";
      port = 2222;
      identityFile = "~/.ssh/id_ed25519";
      identitiesOnly = true;
    };
  };

  programs.direnv = {
    enable = true;
    nix-direnv.enable = true;
  };
}
