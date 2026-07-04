# home-manager config for the codex pod. Baked into the image at build time
# (its home-files are copied into /home/codex), so the pod needs no runtime
# bootstrap script for static config. Secrets are NOT here — they come from k8s
# (BUILDBUDDY_API_KEY env, the id_ed25519 plant, ESO-templated files); so no
# sops-nix, no systemd, non-root.
{ pkgs, ... }:
{
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

  # Codex runs fully unattended in this isolated agent pod — never prompt, no
  # sandbox — mirroring agent-box's `ducktape.codex` (nix/home/hosts/agent-box/
  # codex.nix). The upstream programs.codex module writes config.toml from a
  # home-manager *activation* script (merge.py), but this image bakes only the
  # static home-files and never runs activation — so we bake config.toml directly.
  # Codex reads it from its default CODEX_HOME (~/.codex).
  home.file.".codex/config.toml".source = (pkgs.formats.toml { }).generate "codex-config.toml" {
    model = "gpt-5.5";
    model_reasoning_effort = "xhigh";
    approval_policy = "never";
    sandbox_mode = "danger-full-access";
    history.persistence = "save-all";
    features = {
      streamable_shell = true;
      unified_exec = true;
      apply_patch_freeform = true;
      shell_tool = true;
      view_image_tool = true;
    };
    shell_environment_policy = {
      "inherit" = "all";
      set.CODEX_AGENT = "1";
    };
  };
}
