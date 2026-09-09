# The primary account as a *person's* account, plus the machine settings that only make sense
# when someone administers the box from a keyboard.
#
# Split out of base.nix because `username` names two different things across this flake. On
# wyrm2/rugged/iguana/gecko/bootstrap/cpap-gateway it is the operator. On the agent VMs --
# agent-box's `codex`, public-coder-devbox's `coder` -- it is the confinement target an agent
# lands in. While base.nix granted `wheel` and Nix `trusted-users` to whatever `username` held,
# those accounts were built as operators, and `haku/docs/security.md` invariant #9 described them
# as unprivileged while the configuration said otherwise.
#
# Importing this is now how a host says a human lives on it.
{
  pkgs,
  lib,
  username,
  ...
}:
{
  # No password is set here; `passwd` on first boot. That is why sudo below asks for one.
  users.users.${username} = {
    isNormalUser = true;
    home = "/home/${username}";
    description = username;
    extraGroups = [
      "wheel"
      "networkmanager"
      "video"
      "audio"
    ];
  };

  # Root-equivalent, not a convenience: a trusted Nix user can override daemon settings on its own
  # connections, so this belongs with `wheel` rather than with the flakes/GC settings in base.nix.
  nix.settings.trusted-users = [
    username
    "root"
  ];

  security.sudo.wheelNeedsPassword = true;
  security.sudo.extraConfig = lib.mkAfter ''
    # Show asterisks while typing sudo passwords.
    Defaults pwfeedback
  '';

  # Overkill for a single virtio NIC, which is why `bootstrap` used to mkForce it back off.
  networking.networkmanager.enable = lib.mkDefault true;

  # Lets whoever is debugging the machine read kernel logs without sudo. Deliberately not a
  # default for hosts that exist to confine something.
  boot.kernel.sysctl."kernel.dmesg_restrict" = 0;

  # Interactive conveniences. Machine-facing tools (git, curl, openssl) stay in base.nix.
  environment.systemPackages = with pkgs; [
    vim
    wget
  ];
}
