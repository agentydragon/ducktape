# CPAP ez Share WiFi gateway — a minimal KubeVirt VM pinned to the OptiPlex
# that owns the physical USB WiFi adapter.
{
  cpap-gateway-health,
  lib,
  pkgs,
  ...
}:
let
  secretDevice = "/dev/disk/by-id/virtio-cpapsecret";
  secretMount = "/run/cpap-secret";
  ssid = "Rai CPAP ez Share";
  # Present only once the card's DHCP lease is up: KubeVirt's masquerade default route also
  # answers `ip route get 192.168.4.1`, so that query cannot tell WiFi state.
  cardSubnet = "192.168.4.0/24";
  hasCardRoute = pkgs.writeShellScript "cpap-gateway-has-card-route" ''
    ${pkgs.iproute2}/bin/ip -4 route show ${cardSubnet} | ${pkgs.gnugrep}/bin/grep -q .
  '';
in
{
  imports = [
    ../../nix/nixos/modules/operator.nix
    ../../nix/nixos/modules/vm-hardware.nix
  ];

  # MT7921U firmware and driver for the USB adapter passed through by KubeVirt.
  hardware.firmware = [ pkgs.linux-firmware ];
  boot.kernelModules = [ "mt7921u" ];
  services.udev.extraRules = ''
    ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="0e8d", ATTR{idProduct}=="7961", TEST=="power/control", ATTR{power/control}="on"
  '';

  # KubeVirt's guest-console-log sidecar captures the serial port; without these only the
  # firmware's output reaches Loki.
  boot.kernelParams = [ "console=ttyS0,115200" ];
  services.journald.console = "/dev/ttyS0";

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

  # The Kubernetes Secret, attached by KubeVirt as a read-only disk. cpap-gateway-health
  # reads the card's passphrase from it into an in-memory NetworkManager profile.
  fileSystems.${secretMount} = {
    device = secretDevice;
    fsType = "iso9660";
    options = [
      "ro"
      "nofail"
    ];
  };

  # The cluster Service reaches this listener through KubeVirt's masquerade
  # port forwarding. cpap-gateway-health starts it only while the card's WiFi is
  # connected, so a sync against an unreachable card is refused at once instead
  # of hanging until its read timeout.
  systemd.services.cpap-card-proxy = {
    description = "Expose the CPAP card HTTP API to the cluster";
    serviceConfig = {
      ExecStartPre = hasCardRoute;
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
      # ExecStartPre's `ip route show` queries the kernel through rtnetlink.
      RestrictAddressFamilies = [
        "AF_INET"
        "AF_INET6"
        "AF_NETLINK"
      ];
    };
  };

  systemd.services.cpap-gateway-health = {
    description = "Classify CPAP gateway WiFi health and recover a wedged adapter";
    after = [ "NetworkManager.service" ];
    unitConfig.RequiresMountsFor = secretMount;
    # Every minute: add the card's NetworkManager profile if missing, start the card
    # bridge while connected, retry association while the card is visible, and reset a
    # wedged adapter (cpap/gateway/health.py).
    path = [ pkgs.kmod ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = lib.escapeShellArgs [
        (lib.getExe cpap-gateway-health)
        "--card-ssid=${ssid}"
        "--psk-file=${secretMount}/wifi_password"
        "--connection=cpap-ezshare"
        "--proxy-unit=cpap-card-proxy.service"
        "--driver=mt7921u"
        "--usb-id=0e8d:7961"
        "--state-file=/run/cpap-gateway-health/state.json"
      ];
    };
  };
  systemd.timers.cpap-gateway-health = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "1min";
      OnUnitInactiveSec = "1min";
    };
  };
}
