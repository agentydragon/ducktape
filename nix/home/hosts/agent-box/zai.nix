# zai agent user on agent-box: Claude Code routed to z.ai's GLM via the cluster
# LiteLLM proxy (Anthropic /v1/messages shape, model glm-5.2-anthropic). This user
# holds only the z.ai-scoped LiteLLM virtual key (LITELLM_ZAI_KEY), never the raw
# z.ai key (which stays cluster-side as litellm-zai-key). See ./common.nix for the
# shared base.
#
# Claude Code is configured MINIMALLY here — NOT via ../../claude_code (that module
# carries ~260 workstation permission rules, MCP servers, skills, sandbox defaults
# etc. that are wrong for a fully-open unattended agent VM). This user runs everything
# without prompts, sandbox, or permission checks.
{
  pkgs,
  pkgsUnstable,
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
  ];

  # z.ai-scoped LiteLLM virtual key (SSOT in tf/gitops/litellm-keys/litellm-zai-clients-key.yaml,
  # shared with the laptop z-claude alias). LiteLLM's Anthropic /v1/messages routes to
  # z.ai GLM; the raw z.ai key stays cluster-side. Exported as an env var so the
  # z-claude wrapper below (and z-claude.nix) can read it.
  ducktape.sopsEnv.LITELLM_ZAI_KEY = {
    sopsFile = ../../../../tf/gitops/litellm-keys/litellm-zai-clients-key.yaml;
    key = "litellm_zai_key";
  };

  # Minimal Claude Code config for the agent-box zai user — fully open, unattended.
  programs.claude-code = {
    enable = true;
    package = pkgsUnstable.claude-code;
    settings = {
      theme = "auto";
      permissions.defaultMode = "bypassPermissions";
      permissions.skipDangerousModePermissionPrompt = true;
      sandbox.enabled = false;
    };
  };

  home.packages = [
    # z-claude: same wrapper the laptops use — Claude Code on glm-5.2-anthropic via
    # LiteLLM, reading $LITELLM_ZAI_KEY. See ../../claude_code/z-claude.nix.
    zClaude
  ];
}
