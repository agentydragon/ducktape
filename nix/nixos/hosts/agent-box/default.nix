# agent-box - headless CLI-only NixOS VM (KubeVirt) hosting agent users.
# The codex user runs OpenAI Codex under a dedicated, scoped identity.
# See plans/agent-box.md.
# TODO: add `claude` and `z-claude` agent users on this host (own login keys,
# own scoped secrets, own home dirs).
{
  pkgs,
  lib,
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
  ];

  # CLEANUP(added 2026-06-27): enable the Attic substituter once the
  # attic-jwt-rotation CronJob (rotators.json -> `agent-box`) has minted and
  # committed secrets/hosts/agent-box-attic.yaml to devel. Until that file
  # exists, the path literal below would fail flake evaluation, so the wiring
  # waits:
  #   imports += ../../modules/attic-substituter.nix
  #   ducktape.attic-substituter = {
  #     enable = true;
  #     sopsFile = ../../../../secrets/hosts/agent-box-attic.yaml;
  #   };

  # Passwordless sudo for read-only system inspection commands used by agents.
  ducktape.systemInspectionSudo.enable = true;

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
  # tmpfiles pre-creates the dir codex-owned so home-manager can also write into it.
  systemd.tmpfiles.rules = [ "d /home/${username}/.ssh 0700 ${username} users - -" ];
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
  };

  users.users.root.openssh.authorizedKeys.keys = loginKeys;
  services.openssh.hostKeys = lib.mkForce [
    {
      type = "ed25519";
      path = "/etc/ssh/ssh_host_ed25519_key";
    }
  ];
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  users.motd = "agent-box - headless KubeVirt agent VM (codex user: OpenAI Codex)\n";
}
