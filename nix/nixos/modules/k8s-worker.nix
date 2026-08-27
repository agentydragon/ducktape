# Kubernetes worker node module
# Joins a NixOS machine as a worker to an external (Talos) K8s cluster
# via Nebula mesh overlay.
#
# Does NOT use services.kubernetes — it's designed as a self-contained cluster
# provisioner, not for joining external clusters. Specifically:
#   - Custom CFSSL-based PKI (no --bootstrap-kubeconfig support)
#   - Forces flannel as CNI and wipes /opt/cni/bin on every kubelet start
#   - Deploys its own CoreDNS addon (conflicts with existing cluster DNS)
#   - Kubelet unit depends on local kube-apiserver.service
#   - Builds/seeds a custom pause container instead of registry.k8s.io/pause
#
# API server access: haproxy on 127.0.0.1:7445 load-balances across all control
# plane Nebula IPs with TCP health checks (replaces kubeprism).
#
# Credential placement: sops-nix decrypts the bootstrap token and renders the
# bootstrap kubeconfig via sops.templates. The local haproxy is the API endpoint.
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
  secretsDir = ../../../secrets;
  nebulaDir = secretsDir + "/nebula";
  k8sWorkerFile = secretsDir + "/k8s-worker.yaml";
  hostname = config.networking.hostName;

  # Extract the Nebula IP from the host certificate at build time.
  certInfo = lib.head (
    builtins.fromJSON (
      builtins.readFile (
        pkgs.runCommand "nebula-cert-info" { } ''
          ${pkgs.nebula}/bin/nebula-cert print -path ${nebulaDir}/${hostname}.crt -json > $out
        ''
      )
    )
  );
  nodeIp = lib.head (lib.splitString "/" (lib.head certInfo.details.networks));

  kubeletDeps = with pkgs; [
    kubernetes
    iptables
    socat
    conntrack-tools
    util-linux
    nftables
    tcpdump
    iproute2
    openiscsi # Longhorn requires iscsiadm on the host. TODO(2026-06-09): Longhorn-only — remove once the Longhorn disks are gone (see cluster/terraform/main/proxmox-vms.tf + wyrm2 /var/mnt/longhorn).
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
      # Graceful node shutdown: kubelet listens for systemd's PrepareForShutdown
      # DBus signal, sets the node NotReady, and terminates pods in priority order
      # before releasing the inhibitor lock. Without this, Longhorn engine pods
      # get killed during shutdown, iSCSI targets vanish, and mounted volumes hit
      # I/O errors and EXT4 journal corruption.
      shutdownGracePeriod = "60s";
      shutdownGracePeriodCriticalPods = "15s";
      # Some nodes (e.g. laptops) have swap enabled; don't fail on it.
      failSwapOn = false;
      # Known issue: btrfs nodes log "invalid capacity 0 on image filesystem"
      # at startup because btrfs reports 0 inodes via statfs (dynamic allocation).
      # Harmless — image GC and eviction still work. Workaround if needed:
      # featureGates.LocalStorageCapacityIsolation = false;
      # Default is 110; wyrm2 has run 113+ pods (inventree/ollama/monitoring/...).
      maxPods = 300;
      # NixOS uses systemd-resolved in stub mode, so /etc/resolv.conf has
      # nameserver 127.0.0.53. Point kubelet at the real upstream resolv.conf
      # so all pods (including dnsPolicy:Default like CoreDNS) get real
      # upstreams instead of the stub (else CoreDNS forwards to itself and
      # crash-loops; incident details on the kubelet restartTriggers below).
      resolvConf = "/run/systemd/resolve/resolv.conf";
    }
  );

  # Derive haproxy backends from the mesh SSOT (hosts with role=control-plane).
  # See cluster/docs/mesh_membership.md.
  meshConfig = builtins.fromJSON (builtins.readFile ../../../nebula-mesh.json);
  meshControlPlaneEndpoints = map (h: "${h.nebula_ip}:6443") (
    lib.filter (h: h.role == "control-plane") (lib.attrValues meshConfig.hosts)
  );

  haproxyServerLines = lib.concatStringsSep "\n    " (
    lib.imap1 (
      i: ep: "server cp-${toString i} ${ep} check inter 5s fall 3 rise 2"
    ) cfg.controlPlaneEndpoints
  );
in
{
  imports = [ ./nebula.nix ];

  options.ducktape.k8sWorker = {
    enable = lib.mkEnableOption "Kubernetes worker node (joins external Talos cluster via Nebula mesh)";

    clusterDNS = lib.mkOption {
      type = lib.types.str;
      default = "10.96.0.10";
      description = "Cluster DNS service IP";
    };

    caCertPath = lib.mkOption {
      type = lib.types.str;
      default = "/etc/kubernetes/pki/ca.crt";
      description = "Path to the cluster CA certificate";
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
      default = [ ];
      # Example for roaming/laptop nodes:
      # [ "node-role.kubernetes.io/roaming=true:NoSchedule" ]
      description = "Taints to apply on registration (key=value:effect format)";
    };

    enableNvidiaRuntime = lib.mkEnableOption "NVIDIA GPU support via CDI (Container Device Interface)";

    controlPlaneEndpoints = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = meshControlPlaneEndpoints;
      description = "Control plane API server endpoints (IP:port) for the local haproxy load balancer. Default derived from nebula-mesh.json hosts with role=control-plane.";
    };

  };

  config = lib.mkIf cfg.enable {
    # Plaintext nebula certs deployed via /etc/nebula/
    environment.etc."nebula/ca.crt".text = builtins.readFile (nebulaDir + "/ca.crt");
    environment.etc."nebula/host.crt".text = builtins.readFile (nebulaDir + "/${hostname}.crt");

    # Only the private key needs SOPS decryption (binary format)
    sops.secrets.nebula_host_key = {
      sopsFile = nebulaDir + "/${hostname}.sops.key";
      format = "binary";
    };

    # K8s CA cert is public — deploy via environment.etc
    environment.etc."kubernetes/pki/ca.crt".text = builtins.readFile (secretsDir + "/k8s-ca.crt");

    # Bootstrap token is the only secret in k8s-worker.yaml
    sops.secrets.k8s_bootstrap_token.sopsFile = k8sWorkerFile;

    # Bootstrap kubeconfig rendered by sops-nix with the decrypted token
    sops.templates.bootstrapKubeconfig = {
      content = builtins.toJSON {
        apiVersion = "v1";
        kind = "Config";
        clusters = [
          {
            cluster = {
              certificate-authority = cfg.caCertPath;
              server = "https://127.0.0.1:7445";
            };
            name = "default";
          }
        ];
        contexts = [
          {
            context = {
              cluster = "default";
              user = "kubelet-bootstrap";
            };
            name = "default";
          }
        ];
        current-context = "default";
        users = [
          {
            name = "kubelet-bootstrap";
            user = {
              token = config.sops.placeholder.k8s_bootstrap_token;
            };
          }
        ];
      };
    };

    ducktape.nebulaMesh = {
      caCertPath = "/etc/nebula/ca.crt";
      hostCertPath = "/etc/nebula/host.crt";
      hostKeyPath = config.sops.secrets.nebula_host_key.path;
    };

    # Hide Longhorn iSCSI CSI volumes from UDisks2 so it doesn't offer
    # to manage them. Note: this alone does NOT fix the
    # gvfs-udisks2-volume-monitor CPU burn (~18% on wyrm2). The real
    # cause is GVFS polling /proc/self/mountinfo on every mount event,
    # which is huge on k8s workers (hundreds of containerd overlays).
    # GVFS has no path-based filter for mountinfo. To fix the CPU burn,
    # mask the monitor: systemctl --user mask gvfs-udisks2-volume-monitor
    services.udev.extraRules = ''
      SUBSYSTEM=="block", ENV{ID_VENDOR}=="IET", ENV{ID_MODEL}=="VIRTUAL-DISK", ENV{UDISKS_IGNORE}="1"
    '';

    # iSCSI — required by Longhorn (iscsiadm on host)
    services.openiscsi = {
      enable = true;
      name = "iqn.2020-08.org.nixos:${config.networking.hostName}";
    };

    # Kernel prerequisites for container networking
    boot.kernelModules = [
      "overlay"
      "br_netfilter"
    ];
    boot.kernel.sysctl = {
      "net.bridge.bridge-nf-call-iptables" = 1;
      "net.bridge.bridge-nf-call-ip6tables" = 1;
      "net.ipv4.ip_forward" = 1;
      # Disable reverse path filtering. Cilium manages its own source
      # validation; kernel rp_filter breaks pod-to-node hairpin traffic
      # (e.g., hubble-relay → hubble-peer via ClusterIP routed to local
      # node). The kernel uses max(all, interface) semantics, so per-
      # interface values of 2 override all=0. The wildcard overrides
      # systemd's 50-default.conf which sets conf.*.rp_filter = 2.
      # See: https://docs.cilium.io/en/stable/operations/system_requirements/
      #      https://github.com/cilium/cilium/issues/31565
      "net.ipv4.conf.default.rp_filter" = 0;
      "net.ipv4.conf.all.rp_filter" = 0;
      "net.ipv4.conf.*.rp_filter" = 0;
    };

    # Disable iptables rpfilter (nixos-fw-rpfilter chain in mangle/
    # PREROUTING). nebula.nix sets "loose", but even loose rpfilter
    # drops pod-to-node hairpin traffic. Talos has no iptables rpfilter.
    # See: https://github.com/NixOS/nixpkgs/issues/298165
    networking.firewall.checkReversePath = lib.mkForce false;

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
          # NVIDIA runtime for GPU workloads. Uses CDI specs generated by
          # hardware.nvidia-container-toolkit to inject /dev/nvidia*, driver
          # libs, and glibc into containers. The device plugin DaemonSet uses
          # this runtime (via RuntimeClass) so NVML can discover GPUs.
          containerd.runtimes.nvidia = lib.mkIf cfg.enableNvidiaRuntime {
            runtime_type = "io.containerd.runc.v2";
            options = {
              BinaryName = "${pkgs.nvidia-container-toolkit.tools}/bin/nvidia-container-runtime.cdi";
              SystemdCgroup = true;
            };
          };
          enable_cdi = lib.mkIf cfg.enableNvidiaRuntime true;
          cdi_spec_dirs = lib.mkIf cfg.enableNvidiaRuntime [
            "/etc/cdi"
            "/var/run/cdi"
          ];
          # Cilium DaemonSet installs cilium-cni to /opt/cni/bin.
          # We symlink base CNI plugins there too (see systemd.tmpfiles below).
          cni.bin_dir = "/opt/cni/bin";
          cni.conf_dir = "/etc/cni/net.d";
        };
      };
    };

    # Ensure CDI specs are generated before containerd starts
    systemd.services.containerd = lib.mkMerge [
      (lib.mkIf cfg.enableNvidiaRuntime {
        after = [ "nvidia-container-toolkit-cdi-generator.service" ];
        wants = [ "nvidia-container-toolkit-cdi-generator.service" ];
      })
    ];

    # Cilium DaemonSet installs cilium-cni + loopback into /opt/cni/bin at runtime.
    # We just need the directory to exist.
    systemd.tmpfiles.rules = [ "d /opt/cni/bin 0755 root root -" ];

    environment.systemPackages = kubeletDeps;

    # Kubelet config file
    environment.etc."kubernetes/kubelet-config.yaml".source = kubeletConfigYaml;

    # haproxy TCP proxy for kube-apiserver HA (replaces kubeprism).
    # Load-balances across all control plane Nebula IPs with health checks.
    services.haproxy = {
      enable = true;
      config = ''
        global
          maxconn 1024

        defaults
          mode tcp
          timeout connect 5s
          timeout client 30s
          timeout server 30s
          retries 3

        # haproxy >= 3.3 rejects a frontend and backend sharing a name, so the
        # backend is suffixed. Do not rename them to match.
        frontend kube-apiserver
          bind 127.0.0.1:7445
          default_backend kube-apiserver-backend

        backend kube-apiserver-backend
          option tcp-check
          balance roundrobin
          ${haproxyServerLines}
      '';
    };

    # haproxy needs Nebula up to reach CP nodes. Give all three services the
    # same config trigger so a Nix activation restarts the worker stack in
    # dependency order. Runtime restarts stay explicit rather than using
    # PartOf, which would propagate every transient Nebula stop.
    systemd.services.haproxy = {
      after = [ "nebula.service" ];
      requires = [ "nebula.service" ];
      restartTriggers = [ config.environment.etc."nebula/config.yaml".source ];
    };

    # Kubelet systemd service
    systemd.services.kubelet = {
      description = "Kubernetes Kubelet";
      # Restart kubelet when its config file changes. kubelet reads --config
      # only at startup, and NixOS restarts a service only when its unit
      # changes — so without this trigger a switch that changes the config
      # silently leaves the old one running. Bit us 2026-03-21: kubelet kept a
      # pre-resolvConf config for 9 switches, handed pods the systemd-resolved
      # stub (127.0.0.53), and CoreDNS on NixOS workers crash-looped with
      # 'plugin/loop: Loop (...) detected'. resolvConf applies to
      # dnsPolicy: Default pods too (verified in kubelet/containerd source).
      restartTriggers = [
        kubeletConfigYaml
        config.environment.etc."nebula/config.yaml".source
      ];
      after = [
        "network-online.target"
        "containerd.service"
        "nebula.service"
        "haproxy.service"
      ];
      wants = [
        "network-online.target"
        "containerd.service"
      ];
      # Hard dependencies — kubelet stops if mesh or API proxy dies
      requires = [
        "nebula.service"
        "haproxy.service"
      ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        # Prepend /run/wrappers/bin for NixOS setuid mount/umount wrappers.
        Environment = "PATH=/run/wrappers/bin:${lib.makeBinPath kubeletDeps}:/usr/bin:/bin";
        ExecStart = lib.concatStringsSep " " (
          [
            "${pkgs.kubernetes}/bin/kubelet"
            "--bootstrap-kubeconfig=${config.sops.templates.bootstrapKubeconfig.path}"
            "--kubeconfig=/var/lib/kubelet/kubelet.conf"
            "--config=/etc/kubernetes/kubelet-config.yaml"
            "--node-ip=${nodeIp}"
          ]
          ++
            lib.optional (cfg.nodeLabels != { })
              "--node-labels=${lib.concatStringsSep "," (lib.mapAttrsToList (k: v: "${k}=${v}") cfg.nodeLabels)}"
          ++ lib.optional (
            cfg.nodeTaints != [ ]
          ) "--register-with-taints=${lib.concatStringsSep "," cfg.nodeTaints}"
        );
        Restart = "always";
        RestartSec = "10";
      };
    };

    # Nebula mesh overlay (inter-node connectivity)
    ducktape.nebulaMesh.enable = true;

    # Firewall: allow VXLAN (Cilium) and kubelet
    networking.firewall.allowedUDPPorts = [
      8472 # VXLAN (Cilium overlay)
    ];
    networking.firewall.allowedTCPPorts = [
      10250 # kubelet API
    ];
    # Trust cluster-internal interfaces. Without this, the NixOS firewall
    # drops inter-node and pod-to-node traffic to ports not explicitly
    # opened (hubble-peer 4244, cilium health 4240, etc.). Talos has no
    # host firewall. nebula1 carries all inter-node cluster traffic;
    # cilium_host and lxc* carry pod-to-node traffic (lxc* are Cilium's
    # per-pod veth interfaces on the host side).
    # See: https://github.com/cilium/cilium/issues/31565
    #      https://github.com/NixOS/nixpkgs/issues/437920
    networking.firewall.trustedInterfaces = [
      "nebula1"
      "cilium_host"
      "lxc+"
    ];
  };
}
