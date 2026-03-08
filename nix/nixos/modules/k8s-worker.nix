# Kubernetes worker node module
# Joins a NixOS machine as a worker to an external (Talos) K8s cluster.
# Does NOT use services.kubernetes — it's designed as a self-contained cluster
# provisioner, not for joining external clusters. Specifically:
#   - Custom CFSSL-based PKI (no --bootstrap-kubeconfig support)
#   - Forces flannel as CNI and wipes /opt/cni/bin on every kubelet start
#   - Deploys its own CoreDNS addon (conflicts with existing cluster DNS)
#   - Kubelet unit depends on local kube-apiserver.service
#   - Builds/seeds a custom pause container instead of registry.k8s.io/pause
#
# Credential placement:
#   Cloud-init (via terraform/modules/proxmox-vm) writes bootstrap kubeconfig, CA cert,
#   and kubespand config. Services auto-start on boot.
#
# Manual step after boot:
#   Approve the CSR on the cluster:
#     kubectl get csr
#     kubectl certificate approve <csr-name>
{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.ducktape.k8sWorker;

  isKubespan = cfg.fabric == "kubespan";
  isTailscale = cfg.fabric == "tailscale";

  kubeletDeps = with pkgs; [
    kubernetes
    iptables
    socat
    conntrack-tools
    util-linux
  ];

  kubeletConfigYaml = pkgs.writeText "kubelet-config.yaml" (
    builtins.toJSON {
      kind = "KubeletConfiguration";
      apiVersion = "kubelet.config.k8s.io/v1beta1";
      authentication = {
        anonymous.enabled = false;
        webhook.enabled = true;
        x509.clientCAFile = cfg.caCertPath;
      };
      authorization.mode = "Webhook";
      clusterDomain = "cluster.local";
      clusterDNS = [ cfg.clusterDNS ];
      cgroupDriver = "systemd";
      containerRuntimeEndpoint = "unix:///run/containerd/containerd.sock";
      serverTLSBootstrap = true;
      tlsMinVersion = "VersionTLS12";
    }
  );

  haproxyConfig = ''
    global
      maxconn 256

    defaults
      mode tcp
      timeout connect 5s
      timeout client 30s
      timeout server 30s
      retries 3

    resolvers dns
      nameserver cf 1.1.1.1:53
      nameserver google 8.8.8.8:53
      resolve_retries 3
      timeout resolve 1s
      timeout retry 1s
      hold valid 30s

    frontend kube-apiserver-local
      bind 127.0.0.1:7445
      default_backend kube-apiserver

    backend kube-apiserver
      option tcp-check
      balance roundrobin
      server-template cp 3 ${cfg.apiServerHost}:${toString cfg.apiServerPort} resolvers dns init-addr libc,last check inter 5s fall 3 rise 2
  '';

  # Resolve node IP based on fabric choice
  resolveNodeIp =
    if isKubespan then
      # Read the KubeSpan ULA IPv6 from the already-up kubespan interface
      pkgs.writeShellScript "resolve-kubespan-ip" ''
        ${pkgs.iproute2}/bin/ip -6 addr show dev kubespan scope global \
          | ${pkgs.gnugrep}/bin/grep -oP 'fd[0-9a-f:]+' | ${pkgs.coreutils}/bin/head -1 > /run/kubelet-node-ip
        if [ ! -s /run/kubelet-node-ip ]; then
          echo "Failed to read KubeSpan ULA from kubespan interface" >&2
          exit 1
        fi
      ''
    else
      # Resolve Tailscale IP
      pkgs.writeShellScript "resolve-tailscale-ip" ''
        ${pkgs.tailscale}/bin/tailscale ip --4 | ${pkgs.coreutils}/bin/head -1 > /run/kubelet-node-ip
      '';
in
{
  imports = [ ./kubespand.nix ];

  options.ducktape.k8sWorker = {
    enable = lib.mkEnableOption "Kubernetes worker node (joins external Talos cluster)";

    fabric = lib.mkOption {
      type = lib.types.enum [
        "tailscale"
        "kubespan"
      ];
      default = "tailscale";
      description = ''
        Mesh fabric for inter-node connectivity.
        "tailscale" uses Headscale/Tailscale (separate mesh alongside KubeSpan).
        "kubespan" uses kubespand to join the Talos KubeSpan WireGuard mesh directly.
      '';
    };

    apiServerHost = lib.mkOption {
      type = lib.types.str;
      default = "api.allegedly.works";
      description = "DNS name for the Kubernetes API server (resolved by HAProxy)";
    };

    apiServerPort = lib.mkOption {
      type = lib.types.port;
      default = 6443;
      description = "Port for the Kubernetes API server";
    };

    clusterDNS = lib.mkOption {
      type = lib.types.str;
      default = "10.96.0.10";
      description = "Cluster DNS service IP";
    };

    headscaleUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://headscale.allegedly.works";
      description = "Headscale control server URL for Tailscale (only used with fabric = tailscale)";
    };

    caCertPath = lib.mkOption {
      type = lib.types.str;
      default = "/etc/kubernetes/pki/ca.crt";
      description = "Path to the cluster CA certificate (placed manually)";
    };

    nodeLabels = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = {
        "topology.kubernetes.io/region" = "roaming";
        "node.kubernetes.io/role" = "roaming";
      };
      description = "Labels to apply to the node on registration";
    };

    nodeTaints = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "node-role.kubernetes.io/roaming=true:NoSchedule" ];
      description = "Taints to apply on registration (key=value:effect format)";
    };

  };

  config = lib.mkIf cfg.enable {
    # Kernel prerequisites for container networking
    boot.kernelModules = [
      "overlay"
      "br_netfilter"
    ];
    boot.kernel.sysctl = {
      "net.bridge.bridge-nf-call-iptables" = 1;
      "net.bridge.bridge-nf-call-ip6tables" = 1;
      "net.ipv4.ip_forward" = 1;
    };

    # Containerd
    virtualisation.containerd = {
      enable = true;
      settings = {
        version = 2;
        plugins."io.containerd.grpc.v1.cri" = {
          sandbox_image = "registry.k8s.io/pause:3.10";
          containerd.default_runtime_name = "runc";
          containerd.runtimes.runc = {
            runtime_type = "io.containerd.runc.v2";
            options.SystemdCgroup = true;
          };
          # Cilium DaemonSet installs cilium-cni to /opt/cni/bin.
          # We symlink base CNI plugins there too (see systemd.tmpfiles below).
          cni.bin_dir = "/opt/cni/bin";
          cni.conf_dir = "/etc/cni/net.d";
        };
      };
    };

    # Cilium DaemonSet installs cilium-cni + loopback into /opt/cni/bin at runtime.
    # We just need the directory to exist.
    systemd.tmpfiles.rules = [ "d /opt/cni/bin 0755 root root -" ];

    environment.systemPackages = kubeletDeps;

    # Kubelet config file
    environment.etc."kubernetes/kubelet-config.yaml".source = kubeletConfigYaml;

    # Kubelet systemd service
    systemd.services.kubelet = {
      description = "Kubernetes Kubelet";
      after = [
        "network-online.target"
        "containerd.service"
        "haproxy.service"
      ]
      ++ lib.optional isKubespan "kubespand.service";
      wants = [
        "network-online.target"
        "containerd.service"
      ];
      # kubespand: hard dependency — kubelet stops if kubespand dies
      requires = lib.optional isKubespan "kubespand.service";
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        # Prepend /run/wrappers/bin for NixOS setuid mount/umount wrappers.
        Environment = "PATH=/run/wrappers/bin:${lib.makeBinPath kubeletDeps}:/usr/bin:/bin";
        ExecStartPre = resolveNodeIp;
        ExecStart = pkgs.writeShellScript "kubelet-start" ''
          NODE_IP=$(cat /run/kubelet-node-ip)
          exec ${pkgs.kubernetes}/bin/kubelet \
            --bootstrap-kubeconfig=/etc/kubernetes/bootstrap-kubelet.conf \
            --kubeconfig=/var/lib/kubelet/kubelet.conf \
            --config=/etc/kubernetes/kubelet-config.yaml \
            --node-ip="$NODE_IP" \
            ${
              lib.optionalString (cfg.nodeLabels != { })
                "--node-labels=${lib.concatStringsSep "," (lib.mapAttrsToList (k: v: "${k}=${v}") cfg.nodeLabels)}"
            } \
            ${lib.optionalString (
              cfg.nodeTaints != [ ]
            ) "--register-with-taints=${lib.concatStringsSep "," cfg.nodeTaints}"}
        '';
        Restart = "always";
        RestartSec = "10";
      };
    };

    # HAProxy — replaces KubePrism (localhost:7445 → api.allegedly.works)
    # Cilium agent expects k8sServiceHost=localhost, k8sServicePort=7445.
    # Uses DNS resolution with server-template to discover CP endpoints.
    services.haproxy = {
      enable = true;
      config = haproxyConfig;
    };

    # Tailscale for Headscale mesh connectivity (tailscale fabric only)
    services.tailscale = lib.mkIf isTailscale {
      enable = true;
      extraUpFlags = [
        "--login-server=${cfg.headscaleUrl}"
        "--accept-routes=true"
      ];
      openFirewall = true;
    };

    # KubeSpan fabric: enable kubespand
    ducktape.kubespand = lib.mkIf isKubespan { enable = true; };

    # Firewall: allow VXLAN (Cilium) and kubelet
    networking.firewall.allowedUDPPorts = [
      8472 # VXLAN (Cilium overlay)
    ];
    networking.firewall.allowedTCPPorts = [
      10250 # kubelet API
    ];
  };
}
