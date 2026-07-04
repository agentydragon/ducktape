# home-manager config for the codex pod. Baked into the image at build time
# (its home-files are copied into /home/codex), so the pod needs no runtime
# bootstrap script for static config. Secrets are NOT here — they come from k8s
# (BUILDBUDDY_API_KEY env, the id_ed25519 plant, ESO-templated files); so no
# sops-nix, no systemd, non-root.
{ pkgs, lib, ... }:
let
  keys = import ../../nix/ssh-keys.nix;
  # Humans authorised to `ssh codex-pod` (over `kubectl exec`) — same workstation
  # keys agent-box authorises for inbound login (nix/nixos/hosts/agent-box).
  loginKeys = [
    keys.wyrm2
    keys.atlas
    keys.rugged
  ];
in
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

  # Inbound `ssh codex-pod` over `kubectl exec` (no exposed port): a persistent
  # `sshd -D` listens on 127.0.0.1:2222; clients tunnel to it with a socat relay
  # (see the codex-pod matchBlock in nix/home/home.nix). The transport is already
  # gated by kube RBAC (you can only exec into the pod if allowed); this sshd layer
  # adds pubkey auth so ssh-native tooling (rsync/scp/git/VS Code Remote) works.
  #
  # StrictModes off because ~/.ssh and authorized_keys are read-only /nix/store
  # symlinks; the host key lives on the /workspace PVC (planted at startup) so it's
  # stable across restarts. Non-root sshd => UsePAM off, no privsep user.
  home.file.".ssh/authorized_keys".text = lib.concatStringsSep "\n" loginKeys + "\n";
  home.file.".ssh/sshd_config".text = ''
    Port 2222
    ListenAddress 127.0.0.1
    HostKey /workspace/.sshd/ssh_host_ed25519_key
    AuthorizedKeysFile /home/codex/.ssh/authorized_keys
    AllowUsers codex
    PasswordAuthentication no
    PubkeyAuthentication yes
    StrictModes no
    UsePAM no
    PidFile /tmp/sshd.pid
    Subsystem sftp internal-sftp
    # sshd sanitizes the environment; pass the tools + trust store that the
    # container Env sets, so ssh sessions match `kubectl exec`.
    SetEnv PATH=/bin SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt XDG_CACHE_HOME=/workspace/.cache
  '';

  # nix.conf so flakes work in ssh sessions too (the image's NIX_CONFIG env is not
  # forwarded by sshd; nix reads this file regardless of env).
  home.file.".config/nix/nix.conf".text = ''
    experimental-features = nix-command flakes
    accept-flake-config = true
  '';

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
    # Route Codex at LiteLLM's chatgpt/ (Codex-account) models instead of an
    # interactive ChatGPT sign-in. `env_key` names the env var carrying the
    # LiteLLM virtual key (LITELLM_API_KEY, from the reflected litellm-key-codex-pod
    # secret; see deployment.yaml + tf/gitops/litellm-keys). wire_api=responses:
    # LiteLLM serves these models over the Responses API (streaming-only).
    model = "gpt-5.5-chatgpt";
    model_provider = "litellm";
    model_providers.litellm = {
      name = "Cluster LiteLLM";
      base_url = "https://litellm.allegedly.works/v1";
      env_key = "LITELLM_API_KEY";
      wire_api = "responses";
    };
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
