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

  programs.bash = {
    enable = true;
    # Codex has no global "trust all": its directory-trust prompt is gated
    # per-directory (exact-match `[projects."<path>"]`, no ancestor/global option).
    # The common dirs (~ and /workspace) are pre-trusted in the baked config.toml
    # below; this wrapper covers everything else (e.g. cloned repos) by appending
    # the launch dir (git repo root, else cwd) to config.toml before running codex.
    #
    # Deviation from agent-box: agent-box runs the activation-based `programs.codex`
    # module (../../nix/home/codex/), where `home-manager switch` merges the nix base
    # into config.toml and merge.py's PRESERVE_KEYS keeps the live `projects` block —
    # so codex persists each "Yes" and you answer once per repo. This pod has no
    # activation; instead the baked config.toml is made writable (default.nix) and we
    # pre-trust dirs here. `-c projects."<path>".trust_level=...` does NOT work: codex
    # `-c` keys are naive dotted paths, so the quoted path segment is mis-parsed and
    # never matches the resolved dir. Writing real TOML tables to the file does work
    # (verified: it gates project-local config loading). Isolated YOLO agent pod
    # (danger-full-access, approval=never), so trusting everything is intended.
    initExtra = ''
      codex() {
        local cfg="$HOME/.codex/config.toml" d
        d="$(command git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$PWD")"
        if [ -w "$cfg" ] && ! grep -qF "[projects.\"$d\"]" "$cfg" 2>/dev/null; then
          printf '\n[projects."%s"]\ntrust_level = "trusted"\n' "$d" >>"$cfg"
        fi
        command codex "$@"
      }

      # A real prompt (bash falls back to `-bash-5.3$` with PS1 unset).
      PS1='\[\e[1;36m\]codex-pod\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]\$ '
    '';
  };
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
    # sshd sanitizes the environment, so ssh sessions don't inherit the container's
    # secret env (LITELLM_API_KEY, BUILDBUDDY_API_KEY). The entrypoint writes those
    # into ~/.ssh/environment at startup; read them here so `codex`/`bbr` work over
    # ssh (kubectl exec already inherits the container env).
    PermitUserEnvironment yes
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

  # codex-claude is installed by the image buildEnv. Bake only the minimal
  # unattended Claude Code settings here; its API credential arrives at runtime.
  home.file.".claude/settings.json".text = builtins.toJSON {
    theme = "auto";
    permissions = {
      defaultMode = "bypassPermissions";
      skipDangerousModePermissionPrompt = true;
    };
    sandbox.enabled = false;
  };

  # Codex runs fully unattended in this isolated agent pod — never prompt, no
  # sandbox — mirroring agent-box's `ducktape.codex` (nix/home/hosts/agent-box/
  # codex.nix). The upstream programs.codex module writes config.toml from a
  # home-manager *activation* script (merge.py), but this image bakes only the
  # static home-files and never runs activation — so we bake config.toml directly.
  # Codex reads it from its default CODEX_HOME (~/.codex).
  home.file.".codex/config.toml".source = (pkgs.formats.toml { }).generate "codex-config.toml" {
    # Route Codex at LiteLLM's Codex-subscription models instead of an
    # interactive ChatGPT sign-in. `env_key` names the env var carrying the
    # LiteLLM virtual key (LITELLM_API_KEY, from the reflected litellm-key-codex-pod
    # secret; see deployment.yaml + tf/gitops/litellm-keys). wire_api=responses:
    # LiteLLM's hidden gpt-6-astra alias targets the Responses route. Using the
    # bare slug lets Codex 0.153+ load Astra's bundled model metadata instead of
    # treating the multi-segment LiteLLM route as an unknown model.
    model = "gpt-6-astra";
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
    # Pre-trust the dirs codex is normally launched from (HOME landing dir + the
    # /workspace work root) so its directory-trust prompt never fires there. Other
    # dirs (cloned repos) are appended at launch by the `codex` wrapper above.
    projects = {
      "/home/codex".trust_level = "trusted";
      "/workspace".trust_level = "trusted";
    };
  };
}
