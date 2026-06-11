# Wyrm2 - NixOS dev workstation VM
{
  config,
  pkgs,
  lib,
  username,
  ...
}: {
  imports = [
    ../../modules/gui.nix
    ../../modules/vm-unattended.nix
    ../../modules/dev-workstation.nix
    ../../modules/hm-bootstrap.nix
  ];

  # Timezone
  time.timeZone = "UTC";

  # SSH authorized keys - will be injected by cloud-init initially
  # After first boot, manage via this config
  users.users.${username} = {
    openssh.authorizedKeys.keys = [
      # Add your SSH public key here after initial provisioning
      # "ssh-ed25519 AAAA... user@host"
    ];
    # Allow user to read system logs without sudo
    extraGroups = ["systemd-journal"];
  };
  boot.kernel.sysctl."kernel.dmesg_restrict" = 0;
}
