# zai agent user on agent-box: Claude Code routed to z.ai's GLM via the cluster
# LiteLLM proxy (Anthropic /v1/messages shape, model glm-5.2-anthropic) — NOT z.ai
# directly. So this user holds only the z.ai-scoped LiteLLM virtual key
# (LITELLM_ZAI_KEY), never the raw z.ai key (which stays cluster-side as the
# litellm-zai-key secret). See ./common.nix for the shared base; this file adds
# Claude Code + the zai wrapper + unattended config.
{
  pkgs,
  lib,
  ...
}:
let
  zClaude = import ../../claude_code/z-claude.nix { inherit pkgs; };
in
{
  imports = [
    (import ./common.nix {
      username = "zai";
      homeDirectory = "/home/zai";
      gitName = "zai";
      gitEmail = "zai@allegedly.works";
      kubeconfigUser = "agent-box-zai";
      forgejoKeySopsFile = ../../../../ssh_keys/agent-box-zai-forgejo.sops.key;
      kubeJwtSopsFile = ../../../../secrets/agent-box-zai-k8s-jwt.yaml;
    })
    ../../claude_code # Claude Code CLI + skills/config
  ];

  # z.ai-scoped LiteLLM virtual key (SSOT in tf/gitops/litellm-keys/litellm-zai-clients-key.yaml,
  # shared with the laptop z-claude alias). LiteLLM's Anthropic /v1/messages routes to
  # z.ai GLM; the raw z.ai key stays cluster-side. Exported as an env var so the
  # z-claude wrapper below (and z-claude.nix) can read it.
  ducktape.sopsEnv.LITELLM_ZAI_KEY = {
    sopsFile = ../../../../tf/gitops/litellm-keys/litellm-zai-clients-key.yaml;
    key = "litellm_zai_key";
  };

  # Isolated agent VM: run Claude Code fully unattended, mirroring codex's
  # approvalPolicy="never" + danger-full-access. mkForce overrides the workstation
  # defaults in ../../claude_code (defaultMode="default", sandbox.enabled=true).
  programs.claude-code.settings = {
    permissions.defaultMode = lib.mkForce "bypassPermissions";
    permissions.skipDangerousModePermissionPrompt = true;
    sandbox.enabled = lib.mkForce false;
  };

  home.packages = [
    # z-claude: same wrapper the laptops use (nix/home/home.nix) — Claude Code on
    # glm-5.2-anthropic via LiteLLM, reading $LITELLM_ZAI_KEY. See
    # ../../claude_code/z-claude.nix.
    zClaude
  ];
}
