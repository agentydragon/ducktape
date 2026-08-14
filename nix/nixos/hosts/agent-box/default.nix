# agent-box - headless CLI-only NixOS VM (KubeVirt) hosting agent users, each under
# its own dedicated, scoped identity. `codex` runs OpenAI Codex. See
# cluster/k8s/agent-box/README.md.
#
# Multi-user: the host config is generated from `agentUsers` below. Add a future
# agent user (e.g. `claude`) by appending an entry here + its HM module under
# nix/home/hosts/agent-box/ + its identity material (ssh_keys, .sops.yaml, Authentik
# SA, JWT rotation, RBAC group).
{
  pkgs,
  lib,
  config,
  ...
}:
let
  keys = import ../../../ssh-keys.nix;
  # Humans authorised to log in AS an agent user. An agent user's own key
  # (agent-box-<user>-user) is for outbound git/age, never inbound login.
  loginKeys = with keys; [
    wyrm2
    atlas
    rugged
  ];
  # One entry per agent user. Each gets its own SSH/age identity (planted by NixOS
  # sops-nix), home dir, NixOS user, and (via flake.nix inlineHomeManagerUsers) its
  # own home-manager module under nix/home/hosts/agent-box/<name>.nix.
  agentUsers = [
    {
      name = "codex";
      idSecretPath = ../../../../ssh_keys/agent-box-codex-user.sops.key;
    }
  ];
in
{
  imports = [
    ../../modules/vm-hardware.nix
    ../../modules/bazel
    ../../modules/system-inspection-sudo.nix
    ../../modules/attic-substituter.nix
  ];

  # Pull from cache.allegedly.works/{main,gaffer}. Reader JWT auto-rotated by the
  # attic-jwt-rotation CronJob (rotators.json -> `agent-box`), decryptable by the
  # host key (agent-box-host) + each agent user key.
  ducktape.attic-substituter = {
    enable = true;
    sopsFile = ../../../../secrets/hosts/agent-box-attic.yaml;
  };

  # Process KubeVirt's NoCloud seed to install the persisted Ed25519 host key
  # (agent-box-host) before sshd starts — sops-nix decrypts each agent user key via
  # that host key. Required because agent-box boots its OWN qcow2 image directly (no
  # bootstrap image first), so unlike gecko it must run cloud-init itself; otherwise
  # sshd self-generates a host key sops-nix can't match.
  # Mirrors nix/nixos/hosts/bootstrap/default.nix.
  services.cloud-init = {
    enable = true;
    network.enable = false;
    settings.datasource_list = [ "NoCloud" ];
  };

  # Passwordless sudo for read-only system inspection commands used by agents.
  ducktape.systemInspectionSudo.enable = true;

  zramSwap = {
    enable = true;
    algorithm = "zstd";
    memoryPercent = 200;
    memoryMax = 8 * 1024 * 1024 * 1024;
    priority = 100;
  };

  environment.systemPackages = with pkgs; [
    htop
    btop
    ripgrep
    fd
    fzf
    jq
    yq
    tree
    pv
    strace
    lsof
    git
    tea
    sops
    ssh-to-age
    home-manager
  ];

  # Each agent user's own SSH/age identity (agent-box-<user>-user), encrypted to the
  # VM's host key. NixOS sops-nix decrypts it via the persisted host key and plants
  # it at ~/.ssh/id_ed25519; home-manager (user=<name>) then chains its own sops-nix
  # secrets (BuildBuddy, attic, Forgejo bot key) off this id.
  # tmpfiles pre-creates each ~/.ssh <user>-owned so home-manager can write into it.
  systemd.tmpfiles.rules = map (u: "d /home/${u.name}/.ssh 0700 ${u.name} users - -") agentUsers;
  sops.secrets = builtins.listToAttrs (
    map (
      u:
      lib.nameValuePair "${u.name}_id_ed25519" {
        sopsFile = u.idSecretPath;
        format = "binary";
        path = "/home/${u.name}/.ssh/id_ed25519";
        owner = u.name;
        mode = "0600";
      }
    ) agentUsers
  );

  users.users =
    (builtins.listToAttrs (
      map (
        u:
        lib.nameValuePair u.name {
          isNormalUser = true;
          home = "/home/${u.name}";
          shell = pkgs.zsh;
          openssh.authorizedKeys.keys = loginKeys;
          extraGroups = [ "systemd-journal" ];
          # home-manager installs the user-level sops secrets (BuildBuddy, Forgejo,
          # attic) via the agent user's `sops-nix.service` systemd *user* unit. A
          # headless, non-lingering user has no `systemd --user`, so that unit never
          # starts and the secrets never land. Linger keeps the user manager up at boot.
          # TODO(better): can home-manager sops install user secrets without a
          #   lingering session (activation-time install, not a user service)? Linger
          #   is a heavy hammer for "run one oneshot at boot".
          linger = true;
        }
      ) agentUsers
    ))
    // {
      root.openssh.authorizedKeys.keys = loginKeys;
    };
  services.openssh.hostKeys = lib.mkForce [
    {
      type = "ed25519";
      path = "/etc/ssh/ssh_host_ed25519_key";
    }
  ];
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  # First-boot ordering fix (ugly but localized). On a cold first boot the NixOS
  # `setupSecrets` activation snippet runs pre-systemd, before cloud-init writes the
  # persisted host key (cloud-config stage), so it can't decrypt the <user>_id_ed25519
  # secrets. And each home-manager-<name>.service runs at boot before the lingering
  # user systemd manager is up, so it can't start that user's sops-nix.service either.
  # Once cloud-init has settled, re-apply both: re-run setupSecrets (plants every
  # id_ed25519, host key now present), then restart each user's home-manager service
  # (re-runs `systemctl restart --user sops-nix` with the user manager up + id present
  # → user secrets). Idempotent on reboot.
  #
  # NOTE: system sops install is the `setupSecrets` activation snippet, NOT a systemd
  # unit — there is no sops-install-secrets.service to restart.
  #
  # TODO(better): this re-run is a workaround for sops-runs-early vs
  #   cloud-init-writes-host-key-late. Cleaner options to evaluate:
  #   (a) deliver the host key *before* sysinit (cloud-init `bootcmd` in the
  #       cloud-init-local stage, or an initrd write) so system sops decrypts on
  #       the first try and no re-run is needed;
  #   (b) a sops-nix option to defer secret install past cloud-init without the
  #       Before=sysinit ordering cycle;
  #   (c) accept bootstrap+switch for hosts that need sops at first boot.
  systemd.services.agent-box-secrets-after-cloud-init = {
    description = "Re-apply sops secrets after cloud-init persisted the host key (first-boot race)";
    after = [ "cloud-final.service" ] ++ (map (u: "home-manager-${u.name}.service") agentUsers);
    wants = [ "cloud-final.service" ];
    wantedBy = [ "multi-user.target" ];
    path = [ config.systemd.package ];
    serviceConfig.Type = "oneshot";
    script = ''
      ${config.system.activationScripts.setupSecrets.text}
      ${lib.concatMapStringsSep "\n" (u: "systemctl restart home-manager-${u.name}.service") agentUsers}
    '';
  };

  users.motd = "agent-box - headless KubeVirt VM (codex: OpenAI Codex)\n";
}
