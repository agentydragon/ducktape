# codex agent user on agent-box: OpenAI Codex CLI under a dedicated, scoped identity.
# See ./common.nix for the shared base; this file adds Codex, Claude Code routed
# through CLIProxyAPI, and unattended config for both CLIs.
{
  pkgs,
  pkgsUnstable,
  ...
}:
let
  codexClaude = import ../../claude_code/codex-claude.nix { inherit pkgs; };
in
{
  imports = [
    (import ./common.nix {
      username = "codex";
      homeDirectory = "/home/codex";
      gitName = "codex";
      gitEmail = "codex@allegedly.works";
      kubeconfigUser = "agent-box-codex";
      forgejoKeySopsFile = ../../../../ssh_keys/agent-box-codex-forgejo.sops.key;
      forgejoTeaSopsFile = ../../../../secrets/agent-box-codex-forgejo-tea-token.yaml;
      kubeJwtSopsFile = ../../../../secrets/agent-box-codex-k8s-jwt.yaml;
    })
    ../../codex # OpenAI Codex CLI + config
  ];

  # Isolated agent VM: run Codex fully unattended — no prompts, no sandbox.
  ducktape.codex = {
    approvalPolicy = "never";
    sandboxMode = "danger-full-access";
  };

  # Same CLIProxyAPI client-key SSOT used by workstations and reflected into
  # codex-pod. The agent-box codex identity is an explicit SOPS recipient.
  ducktape.sopsEnv.CLIPROXY_CLIENT_KEY = {
    sopsFile = ../../../../cluster/k8s/cli-proxy-api/client-key.sops.yaml;
    key = "stringData/client-key";
  };

  # Minimal Claude Code config for this unattended agent identity. Do not import
  # the workstation module, whose prompts, MCP servers, and desktop integrations
  # are inappropriate here.
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

  home.packages = [ codexClaude ];
}
