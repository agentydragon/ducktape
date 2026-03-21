# lxc-k8s-test — NixOS LXC container on Proxmox (atlas)
# Test container for running a k8s worker node in LXC.
# Joins the Talos k8s cluster via Nebula mesh (like wyrm2/rugged).
#
# Requires privileged LXC for: containerd (nesting), iSCSI.
# Host kernel modules (overlay, br_netfilter, iscsi_tcp) must
# be loaded on atlas — managed by ansible/atlas.yaml.
#
# K8s credentials (/etc/kubernetes/pki/ca.crt,
# /etc/kubernetes/bootstrap-kubelet.conf) are placed manually after boot.
{
  config,
  pkgs,
  lib,
  username,
  modulesPath,
  ...
}:
{
  imports = [
    (modulesPath + "/virtualisation/proxmox-lxc.nix")
    ../../modules/dev-workstation.nix
    ../../modules/k8s-worker.nix
  ];

  # Proxmox LXC settings
  proxmoxLXC = {
    privileged = true;
    manageHostName = true; # Let NixOS set hostname (base.nix sets networking.hostName)
  };

  # Override base.nix bootloader settings — LXC has no bootloader (host kernel).
  # proxmox-lxc.nix sets boot.isContainer = true and boot.loader.initScript.enable = true.
  boot.loader.systemd-boot.enable = lib.mkForce false;
  boot.loader.efi.canTouchEfiVariables = lib.mkForce false;

  # proxmox-lxc.nix enables systemd-networkd; NetworkManager from base.nix conflicts.
  networking.networkmanager.enable = lib.mkForce false;

  time.timeZone = "America/Los_Angeles";

  # K8s worker — credentials placed manually after boot
  ducktape.k8sWorker = {
    enable = true;
    nodeLabels = {
      "topology.kubernetes.io/region" = "proxmox";
      "topology.kubernetes.io/zone" = "atlas";
      "node.kubernetes.io/instance-type" = "lxc";
    };
  };

  users.users.${username} = {
    shell = pkgs.zsh;
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFfzLZ7zOOMviYrrxeh1nSXdwu9uveSXr07EJI5NwFau agentydragon@wyrm"
    ];
    extraGroups = [ "systemd-journal" ];
  };

  users.users.root.openssh.authorizedKeys.keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFfzLZ7zOOMviYrrxeh1nSXdwu9uveSXr07EJI5NwFau agentydragon@wyrm"
  ];
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  # Passwordless sudo for test container
  security.sudo.wheelNeedsPassword = lib.mkForce false;

  programs.zsh.enable = true;

  boot.kernel.sysctl."kernel.dmesg_restrict" = 0;
}
