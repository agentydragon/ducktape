# codex agent user on agent-box: OpenAI Codex CLI under a dedicated, scoped identity.
# See ./common.nix for the shared base; this file adds the Codex CLI + unattended config.
{ ... }:
{
  imports = [
    (import ./common.nix {
      username = "codex";
      homeDirectory = "/home/codex";
      gitName = "codex";
      gitEmail = "codex@allegedly.works";
      kubeconfigUser = "agent-box-codex";
      forgejoKeySopsFile = ../../../../ssh_keys/agent-box-codex-forgejo.sops.key;
      kubeJwtSopsFile = ../../../../secrets/agent-box-codex-k8s-jwt.yaml;
    })
    ../../codex # OpenAI Codex CLI + config
  ];

  # Isolated agent VM: run Codex fully unattended — no prompts, no sandbox.
  ducktape.codex = {
    approvalPolicy = "never";
    sandboxMode = "danger-full-access";
  };
}
