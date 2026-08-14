# CPAP ez Share WiFi gateway — a minimal KubeVirt VM pinned to the OptiPlex
# that owns the physical USB WiFi adapter.
{
  lib,
  pkgs,
  ...
}:
let
  secretDevice = "/dev/disk/by-id/virtio-cpapsecret";
  waitForCardRoute = pkgs.writeShellScript "cpap-gateway-wait-for-card-route" ''
    set -eu
    for _ in $(seq 1 300); do
      if ${pkgs.iproute2}/bin/ip route get 192.168.4.1 >/dev/null 2>&1; then
        exit 0
      fi
      sleep 1
    done
    echo "CPAP card route did not appear within 5 minutes" >&2
    exit 1
  '';
in
{
  imports = [ ../../modules/vm-hardware.nix ];

  # MT7921U firmware and driver for the USB adapter passed through by KubeVirt.
  hardware.firmware = [ pkgs.linux-firmware ];
  boot.kernelModules = [ "mt7921u" ];
  services.udev.extraRules = ''
    ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="0e8d", ATTR{idProduct}=="7961", TEST=="power/control", ATTR{power/control}="on"
  '';

  networking.networkmanager.enable = true;
  networking.firewall.allowedTCPPorts = [ 18080 ];
  environment.systemPackages = with pkgs; [
    iproute2
    networkmanager
    socat
    util-linux
  ];

  # The gateway has no administrative SSH surface; its only application
  # surface is the CPAP card proxy below.
  services.openssh.enable = lib.mkForce false;

  # Read the encrypted Kubernetes Secret from the KubeVirt-attached virtio
  # disk, create the NetworkManager profile, and associate with the card AP.
  # The disk is mounted only for this bootstrap step; the resulting keyfile is
  # owned by NetworkManager and is needed for automatic reconnects.
  systemd.services.cpap-gateway-wifi = {
    description = "Configure the CPAP ez Share WiFi connection";
    wantedBy = [ "multi-user.target" ];
    wants = [ "NetworkManager.service" ];
    after = [
      "local-fs.target"
      "NetworkManager.service"
    ];
    path = with pkgs; [
      coreutils
      networkmanager
      util-linux
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      Restart = "on-failure";
      RestartSec = 5;
    };
    script = ''
      set -eu
      mountpoint=/run/cpap-secret
      install -d -m 700 "$mountpoint"
      mounted=0
      for _ in $(seq 1 60); do
        if mountpoint -q "$mountpoint"; then
          mounted=1
          break
        fi
        if mount -o ro ${secretDevice} "$mountpoint" 2>/dev/null; then
          mounted=1
          break
        fi
        sleep 1
      done
      if [ "$mounted" -ne 1 ]; then
        echo "KubeVirt CPAP secret disk did not appear at ${secretDevice}" >&2
        exit 1
      fi
      trap 'umount "$mountpoint"' EXIT

      password="$(cat "$mountpoint/wifi_password")"
      test -n "$password"
      nmcli connection delete cpap-ezshare >/dev/null 2>&1 || true
      nmcli connection add \
        type wifi \
        ifname "*" \
        con-name cpap-ezshare \
        ssid "Rai CPAP ez Share" \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$password" \
        ipv4.method auto \
        ipv4.never-default yes \
        ipv6.method disabled
      nmcli connection modify cpap-ezshare connection.autoconnect yes
      nmcli connection up cpap-ezshare
    '';
  };

  # The cluster Service reaches this listener through KubeVirt's masquerade
  # port forwarding. Wait for the card-side route so the listener cannot accept
  # requests that would otherwise fail during WiFi association.
  systemd.services.cpap-card-proxy = {
    description = "Expose the CPAP card HTTP API to the cluster";
    wantedBy = [ "multi-user.target" ];
    wants = [
      "cpap-gateway-wifi.service"
      "network-online.target"
    ];
    after = [
      "cpap-gateway-wifi.service"
      "network-online.target"
    ];
    serviceConfig = {
      ExecStartPre = waitForCardRoute;
      ExecStart = "${pkgs.socat}/bin/socat TCP-LISTEN:18080,bind=0.0.0.0,reuseaddr,fork,keepalive TCP:192.168.4.1:80,keepalive";
      Restart = "always";
      RestartSec = 5;
      User = "nobody";
      Group = "nogroup";
      NoNewPrivileges = true;
      PrivateDevices = true;
      PrivateTmp = true;
      ProtectHome = true;
      ProtectSystem = "strict";
      # ExecStartPre's `ip route get` queries the kernel through rtnetlink.
      RestrictAddressFamilies = [
        "AF_INET"
        "AF_INET6"
        "AF_NETLINK"
      ];
    };
  };
}
