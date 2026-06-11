# Nebula mesh network module for NixOS workers
# Provides Nebula mesh overlay for inter-node connectivity.
#
# Set caCertPath/hostCertPath/hostKeyPath (e.g. to sops-nix secret paths) to
# generate /etc/nebula/config.yaml from Nix. Lighthouse topology is read from
# nebula-mesh.yaml at the repo root (single source of truth).
{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.ducktape.nebulaMesh;
  # Read lighthouse topology from shared config (single source of truth)
  meshConfig = builtins.fromJSON (builtins.readFile ../../../nebula-mesh.json);
  generatedConfig = builtins.toJSON {
    pki = {
      ca = cfg.caCertPath;
      cert = cfg.hostCertPath;
      key = cfg.hostKeyPath;
    };
    static_host_map = cfg.staticHostMap;
    lighthouse = {
      am_lighthouse = false;
      interval = 10;
      hosts = cfg.lighthouses;
      # Block Cilium/container interfaces from being advertised as Nebula
      # endpoints. Without this, Nebula may advertise pod CIDR IPs (10.244.x.x)
      # to peers, causing a VXLAN-in-Nebula tunnel loop.
      # See cluster/debug/2026-04-07-pve-cp0-etcd-partition/
      local_allow_list = {
        interfaces = {
          "cilium.*" = false;
          "lxc.*" = false;
        };
      };
    };
    relay = {
      relays = cfg.lighthouses;
      use_relays = true;
    };
    listen = {
      host = "0.0.0.0";
      port = 4242;
    };
    punchy = {
      punch = true;
      respond = true;
    };
    logging = {
      level = "info";
      format = "json";
    };
    timers = {
      connection_alive_interval = 5;
      pending_deletion_interval = 10;
    };
    tun = {
      dev = "nebula1";
    };
    firewall = {
      outbound = [
        {
          port = "any";
          proto = "any";
          host = "any";
        }
      ];
      inbound = [
        {
          port = "any";
          proto = "any";
          host = "any";
        }
      ];
    };
  };
in
{
  options.ducktape.nebulaMesh = {
    enable = lib.mkEnableOption "Nebula mesh network";

    lighthouses = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = meshConfig.lighthouse_ips;
      description = "Nebula IPs of lighthouse nodes (from nebula-mesh.json)";
    };

    staticHostMap = lib.mkOption {
      type = lib.types.attrsOf (lib.types.listOf lib.types.str);
      default = meshConfig.static_host_map;
      description = "Nebula IP → [hostname:port] mapping for lighthouses (from nebula-mesh.json)";
    };

    caCertPath = lib.mkOption {
      type = lib.types.str;
      description = "Path to Nebula CA cert file (e.g. sops secret path)";
    };

    hostCertPath = lib.mkOption {
      type = lib.types.str;
      description = "Path to host cert file (e.g. sops secret path)";
    };

    hostKeyPath = lib.mkOption {
      type = lib.types.str;
      description = "Path to host key file (e.g. sops secret path)";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ pkgs.nebula ];

    environment.etc."nebula/config.yaml".text = generatedConfig;

    # Nebula mesh UDP port
    networking.firewall.allowedUDPPorts = [ 4242 ];

    # Loose reverse path filter — Nebula TUN traffic can trigger rpfilter
    # checks the same way WireGuard traffic does (decrypted packets arrive
    # on nebula1 with source IPs whose reverse path goes via the physical interface).
    networking.firewall.checkReversePath = "loose";

    # systemd-resolved for split DNS — lighthouse DNS resolves mesh
    # hostnames (cert names under nebula.allegedly.works), normal DNS
    # handles everything else via the routing domain below.
    services.resolved.enable = true;

    # NetworkManager should not touch the Nebula TUN interface.
    networking.networkmanager.unmanaged = [ "nebula1" ];

    # State directory for Nebula
    systemd.tmpfiles.rules = [ "d /var/lib/nebula 0700 root root -" ];

    systemd.services.nebula = {
      description = "Nebula mesh network";
      after = [
        "network-online.target"
        "systemd-resolved.service"
      ];
      wants = [
        "network-online.target"
        "systemd-resolved.service"
      ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        Type = "simple";
        ExecStart = "${pkgs.nebula}/bin/nebula -config /etc/nebula/config.yaml";
        # Configure nebula1 link DNS after the TUN interface is up.
        # Cert names are FQDNs under nebula.allegedly.works. The routing
        # domain ~nebula.allegedly.works tells resolved to send only
        # *.nebula.allegedly.works queries to lighthouse DNS. Public DNS
        # is unaffected even when cluster nodes are down (unlike the old
        # +DefaultRoute approach which broke all DNS on outage).
        ExecStartPost = pkgs.writeShellScript "nebula-dns-setup" ''
          for i in $(seq 1 30); do
            if ${pkgs.iproute2}/bin/ip link show nebula1 &>/dev/null; then
              break
            fi
            sleep 1
          done
          ${pkgs.systemd}/bin/resolvectl dns nebula1 ${lib.concatStringsSep " " cfg.lighthouses}
          ${pkgs.systemd}/bin/resolvectl domain nebula1 ~nebula.allegedly.works
          ${pkgs.systemd}/bin/resolvectl default-route nebula1 false
        '';
        Restart = "on-failure";
        RestartSec = "5";
        # Required capabilities for TUN device management
        AmbientCapabilities = "CAP_NET_ADMIN CAP_NET_RAW";
        CapabilityBoundingSet = "CAP_NET_ADMIN CAP_NET_RAW";
      };
    };
  };
}
