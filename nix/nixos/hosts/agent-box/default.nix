# agent-box - headless CLI-only NixOS VM (KubeVirt) hosting agent users.
# The codex user runs OpenAI Codex under a dedicated, scoped identity.
# See plans/agent-box.md.
# TODO: add `claude` and `z-claude` agent users on this host (own login keys,
# own scoped secrets, own home dirs).
{
  pkgs,
  lib,
  config,
  username,
  ...
}:
let
  keys = import ../../../ssh-keys.nix;
  # Humans authorised to log in AS the codex user. codex's own key
  # (agent-box-codex-user) is for outbound git/age, never inbound login.
  loginKeys = with keys; [
    wyrm2
    atlas
    rugged
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
  # host key (cluster... agent-box-host) + the codex user key.
  ducktape.attic-substituter = {
    enable = true;
    sopsFile = ../../../../secrets/hosts/agent-box-attic.yaml;
  };

  # Process KubeVirt's NoCloud seed to install the persisted Ed25519 host key
  # (agent-box-host) before sshd starts — sops-nix decrypts the codex user key
  # via that host key. Required because agent-box boots its OWN qcow2 image
  # directly (no bootstrap image first), so unlike gecko it must run cloud-init
  # itself; otherwise sshd self-generates a host key sops-nix can't match.
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

  # KubeVirt emptyDisk-backed disposable caches. The root DataVolume stays
  # persistent; these volumes survive guest reboots but not VMI re-creation.
  fileSystems."/home/${username}/.cache" = {
    device = "/dev/disk/by-id/virtio-abox-cache";
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
    options = [
      "nodev"
      "nosuid"
      "nofail"
      "x-systemd.device-timeout=30s"
    ];
  };

  fileSystems."/home/${username}/.cache/nix" = {
    device = "/dev/disk/by-id/virtio-abox-nix-cache";
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
    depends = [ "/home/${username}/.cache" ];
    options = [
      "nodev"
      "nosuid"
      "nofail"
      "x-systemd.device-timeout=30s"
    ];
  };

  environment.systemPackages = with pkgs; [
    neovim
    tmux
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
    sops
    ssh-to-age
    home-manager
  ];

  # The codex user's own SSH/age identity (agent-box-codex-user), encrypted to
  # the VM's host key. NixOS sops-nix decrypts it via the persisted host key and
  # plants it at ~/.ssh/id_ed25519; home-manager (user=codex) then chains its
  # own sops-nix secrets (BuildBuddy, attic, Forgejo bot key) off this id.
  # tmpfiles pre-creates the SSH directory and cache volume mount roots
  # codex-owned so home-manager can write into them. Cache subdirectories are
  # created by the Home Manager modules that own those tools.
  systemd.tmpfiles.rules = [
    "d /home/${username}/.ssh 0700 ${username} users - -"
    "d /home/${username}/.cache 0755 ${username} users - -"
    "z /home/${username}/.cache 0755 ${username} users - -"
    "d /home/${username}/.cache/nix 0755 ${username} users - -"
    "z /home/${username}/.cache/nix 0755 ${username} users - -"
  ];
  sops.secrets.codex_id_ed25519 = {
    sopsFile = ../../../../ssh_keys/agent-box-codex-user.sops.key;
    format = "binary";
    path = "/home/${username}/.ssh/id_ed25519";
    owner = username;
    mode = "0600";
  };

  users.users.${username} = {
    shell = pkgs.zsh;
    openssh.authorizedKeys.keys = loginKeys;
    extraGroups = [ "systemd-journal" ];
    # home-manager installs the user-level sops secrets (BuildBuddy, Forgejo,
    # attic) via the codex user's `sops-nix.service` systemd *user* unit. A
    # headless, non-lingering user has no `systemd --user`, so that unit never
    # starts and the secrets never land. Linger keeps the user manager up at boot.
    # TODO(better): can home-manager sops install user secrets without a
    #   lingering session (activation-time install, not a user service)? Linger
    #   is a heavy hammer for "run one oneshot at boot".
    linger = true;
  };

  users.users.root.openssh.authorizedKeys.keys = loginKeys;
  services.openssh.hostKeys = lib.mkForce [
    {
      type = "ed25519";
      path = "/etc/ssh/ssh_host_ed25519_key";
    }
  ];
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  # First-boot ordering fix (ugly but localized). On a cold first boot the
  # NixOS `setupSecrets` activation snippet runs pre-systemd, before cloud-init
  # writes the persisted host key (cloud-config stage), so it can't decrypt
  # codex_id_ed25519. And home-manager-${username}.service runs at boot before
  # the lingering user systemd manager is up, so it can't start the codex user's
  # sops-nix.service either. Once cloud-init has settled, re-apply both:
  # re-run setupSecrets (plants id_ed25519, host key now present), then restart
  # home-manager (re-runs `systemctl restart --user sops-nix` with the user
  # manager up + id present → user secrets). Idempotent on reboot.
  #
  # NOTE: system sops install is the `setupSecrets` activation snippet, NOT a
  # systemd unit — there is no sops-install-secrets.service to restart.
  #
  # TODO(better): this re-run is a workaround for sops-runs-early vs
  #   cloud-init-writes-host-key-late. Cleaner options to evaluate:
  #   (a) deliver the host key *before* sysinit (cloud-init `bootcmd` in the
  #       cloud-init-local stage, or an initrd write) so system sops decrypts on
  #       the first try and no re-run is needed;
  #   (b) a sops-nix option to defer secret install past cloud-init without the
  #       Before=sysinit ordering cycle;
  #   (c) accept bootstrap+switch for hosts that need sops at first boot.
  #   See plans/agent-box.md and the boot-ordering discussion.
  systemd.services.agent-box-secrets-after-cloud-init = {
    description = "Re-apply sops secrets after cloud-init persisted the host key (first-boot race)";
    after = [
      "cloud-final.service"
      "home-manager-${username}.service"
    ];
    wants = [ "cloud-final.service" ];
    wantedBy = [ "multi-user.target" ];
    path = [ config.systemd.package ];
    serviceConfig.Type = "oneshot";
    script = ''
      ${config.system.activationScripts.setupSecrets.text}
      systemctl restart home-manager-${username}.service
    '';
  };

  users.motd = "agent-box - headless KubeVirt agent VM (codex user: OpenAI Codex)\n";
}
