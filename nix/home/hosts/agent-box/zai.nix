# zai agent user on agent-box: Claude Code routed to z.ai's GLM via the cluster
# LiteLLM proxy (Anthropic /v1/messages shape, model glm-5.2-anthropic) — NOT z.ai
# directly. So this user holds only the LiteLLM master key, never ZAI_API_KEY (which
# stays cluster-side as the litellm-zai-key secret). See ./common.nix for the shared
# base; this file adds Claude Code + the zai wrapper + unattended config.
{
  pkgs,
  lib,
  ...
}:
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

  # LiteLLM master key -> LiteLLM's Anthropic /v1/messages -> z.ai GLM. The token is
  # the in-cluster litellm-master-key value (mirrored into SOPS); z.ai's own key stays
  # cluster-side only. Exported as an env var so the zai wrapper below can read it.
  ducktape.sopsEnv.LITELLM_API_KEY = {
    sopsFile = ../../../../secrets/agent-box-zai-litellm.yaml;
    key = "litellm_api_key";
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
    # zai wrapper: Claude Code against LiteLLM's Anthropic shape, model
    # glm-5.2-anthropic. WebFetch/WebSearch disabled (GLM tool-call shape; see
    # cluster/k8s/litellm/app/generate_litellm.py). Differs from the user-machine
    # z-claude alias (nix/home/home.nix) only in base URL/model/token: LiteLLM +
    # glm-5.2-anthropic + LITELLM_API_KEY instead of z.ai direct + ZAI_API_KEY.
    (pkgs.writeShellScriptBin "zai" ''
      exec env \
        ANTHROPIC_BASE_URL=https://litellm.allegedly.works \
        ANTHROPIC_AUTH_TOKEN="$LITELLM_API_KEY" \
        ANTHROPIC_MODEL=glm-5.2-anthropic \
        claude --disallowed-tools "WebFetch WebSearch" \
        "$@"
    '')
  ];
}
