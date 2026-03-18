# kubespand — NixOS module for the standalone KubeSpan daemon
#
# Manages the kubespand systemd service, optional apid (Talos API proxy),
# WireGuard kernel module, firewall, and IPv6 forwarding. Config file
# (/etc/kubespan/agent.yaml) contains secrets and is placed by cloud-init
# (not in the Nix store).
#
# apid is the Talos gRPC reverse proxy: it connects to kubespand's machined
# Unix socket and exposes the Talos API on port 50000 with mTLS. This lets
# standard `talosctl` commands work against kubespand nodes.
{
  config,
  pkgs,
  lib,
  kubespand-tar,
  ...
}:
let
  cfg = config.ducktape.kubespand;
in
{
  options.ducktape.kubespand = {
    enable = lib.mkEnableOption "kubespand (standalone KubeSpan daemon)";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../packages/kubespand.nix { inherit kubespand-tar; };
      description = "The kubespand binary package";
    };

    configPath = lib.mkOption {
      type = lib.types.str;
      default = "/etc/kubespan/agent.yaml";
      description = "Path to the YAML config file (contains secrets, placed manually)";
    };

    debug = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable debug logging";
    };

    apid = {
      enable = lib.mkEnableOption "apid (Talos API proxy on port 50000 with mTLS)";

      package = lib.mkOption {
        type = lib.types.package;
        default = cfg.package;
        defaultText = lib.literalExpression "cfg.package (same derivation as kubespand)";
        description = "Package containing the apid binary";
      };

      port = lib.mkOption {
        type = lib.types.port;
        default = 50000;
        description = "TCP port for the mTLS gRPC listener (Talos API port)";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    # WireGuard kernel module
    boot.kernelModules = [ "wireguard" ];

    # IPv6 forwarding (KubeSpan ULA addresses)
    boot.kernel.sysctl."net.ipv6.conf.all.forwarding" = 1;

    # Firewall: allow WireGuard UDP port
    networking.firewall.allowedUDPPorts = [ 51820 ];

    # State directory for identity keypair
    systemd.tmpfiles.rules = [ "d /var/lib/kubespan 0700 root root -" ];

    # Firewall: allow apid mTLS port
    networking.firewall.allowedTCPPorts = lib.mkIf cfg.apid.enable [ cfg.apid.port ];

    systemd.services.kubespand = {
      description = "kubespand — standalone KubeSpan daemon";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        Type = "simple";
        ExecStart = lib.concatStringsSep " " (
          [
            "${cfg.package}/bin/kubespand"
            "-config"
            cfg.configPath
          ]
          ++ lib.optional cfg.debug "-debug"
        );

        # Health check: consider started once the kubespan WireGuard interface exists.
        # kubespand creates it shortly after startup.
        ExecStartPost = pkgs.writeShellScript "kubespand-wait-interface" ''
          for i in $(seq 1 30); do
            if ${pkgs.iproute2}/bin/ip link show kubespan >/dev/null 2>&1; then
              exit 0
            fi
            sleep 1
          done
          echo "kubespand: kubespan interface not created within 30s" >&2
          exit 1
        '';

        Restart = "on-failure";
        RestartSec = "5";

        # Graceful shutdown (removes WireGuard interface, nftables rules, deregisters from discovery)
        KillMode = "mixed";
        TimeoutStopSec = 30;

        # State directory
        StateDirectory = "kubespan";
      };
    };

    # apid — Talos API reverse proxy (mTLS on port 50000 → machined Unix socket)
    systemd.services.apid = lib.mkIf cfg.apid.enable {
      description = "apid — Talos API proxy daemon";
      after = [
        "network-online.target"
        "kubespand.service"
      ];
      wants = [ "network-online.target" ];
      # apid proxies to kubespand's Unix socket — it must be running
      requires = [ "kubespand.service" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        Type = "simple";
        ExecStart = "${cfg.apid.package}/bin/apid";

        Restart = "on-failure";
        RestartSec = "5";

        # Graceful shutdown
        KillMode = "mixed";
        TimeoutStopSec = 10;
      };
    };
  };
}
