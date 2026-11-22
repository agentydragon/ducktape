# NixOS guest for the experimental KubeVirt BuildBuddy KVM worker pool.
{ lib, pkgs, ... }:

let
  workerConfig = ../../worker;
  keyMount = "/run/buildbuddy-key";
  configMount = "/run/buildbuddy-config";
  workMount = "/buildbuddy";
  executorImageName = "buildbuddy.bbcr.io/public/buildbuddy-executor-enterprise";
  executorImageDigest = "sha256:465d670159f7f2be756eb31815d7c6be54ce812de74a5cc49d3f6bda1fc7b078";
  executorImageTag = "sha256-465d670159f7f2be756eb31815d7c6be54ce812de74a5cc49d3f6bda1fc7b078";
  executorImage = pkgs.dockerTools.pullImage {
    imageName = executorImageName;
    imageDigest = executorImageDigest;
    hash = "sha256-2OAUwYCy2tItyl1Qa/PHsyN8cFLDUPpDT37OrC6XZAE=";
    finalImageName = executorImageName;
    finalImageTag = executorImageTag;
  };
  executorRunner = pkgs.writeShellScript "buildbuddy-kvm-worker-run-executor" (
    builtins.readFile ./run-executor.sh
  );
  executorImageLoader = pkgs.writeShellScript "buildbuddy-kvm-worker-load-executor-image" ''
    if ${pkgs.docker}/bin/docker image inspect ${lib.escapeShellArg "${executorImageName}:${executorImageTag}"} >/dev/null 2>&1; then
      exit 0
    fi
    ${pkgs.docker}/bin/docker image load --input ${executorImage}
    ${pkgs.docker}/bin/docker image inspect ${lib.escapeShellArg "${executorImageName}:${executorImageTag}"} >/dev/null
  '';
  runtimeK3sConfig = pkgs.runCommand "buildbuddy-kvm-worker-k3s-config" { } ''
    ln -s /run/buildbuddy-kvm-worker-k3s "$out"
  '';
  guestModules = lib.filter (module: module != "") (
    lib.splitString "\n" (builtins.readFile "${workerConfig}/modules-load.conf")
  );
in
{
  imports = [ ../../../../../nix/nixos/modules/vm-hardware.nix ];

  networking.hostName = "buildbuddy-kvm-worker-spike";
  networking.firewall.allowedTCPPorts = [
    8080
    9090
    9100
  ];
  services.openssh.enable = lib.mkForce false;
  services.journald.console = "/dev/ttyS0";
  services.qemuGuest.enable = true;
  services.timesyncd = {
    servers = [ "time.cloudflare.com" ];
    fallbackServers = [ ];
  };
  services.prometheus.exporters.node.enable = true;

  documentation.enable = false;
  virtualisation.diskSize = 8 * 1024;
  # The pinned 6.18 kernel config disables legacy xtables, which the K3s test
  # fixture's kube-proxy requires. Use the stock NixOS LTS package with NAT.
  boot.kernelPackages = pkgs.linuxPackages_6_12;
  boot.loader.timeout = 0;
  boot.kernelParams = [
    "apparmor=0"
    "console=ttyS0,115200n8"
    "loop.max_loop=64"
  ];
  # vm-hardware's generic list cannot select the guest CPU vendor. Load the
  # matching nested-KVM module in the prerequisite unit below instead.
  boot.kernelModules = lib.mkForce guestModules;
  environment.etc."modprobe.d/buildbuddy-kvm-worker.conf".source = "${workerConfig}/modprobe.conf";
  environment.etc."modules-load.d/buildbuddy-kvm-worker.conf".source =
    "${workerConfig}/modules-load.conf";
  environment.etc."sysctl.d/90-buildbuddy-kvm-worker.conf".source = "${workerConfig}/sysctl.conf";
  environment.etc."docker/buildbuddy-seccomp.json".source = "${workerConfig}/docker-seccomp.json";
  environment.etc."rancher/k3s".source = runtimeK3sConfig;

  fileSystems.${workMount} = {
    device = "/dev/disk/by-id/virtio-work";
    fsType = "ext4";
    options = [ "noatime" ];
  };
  fileSystems.${keyMount} = {
    device = "/dev/disk/by-id/virtio-bbkey";
    fsType = "iso9660";
    options = [
      "ro"
      "nofail"
    ];
  };
  fileSystems.${configMount} = {
    device = "/dev/disk/by-id/virtio-bbconfig";
    fsType = "iso9660";
    options = [
      "ro"
      "nofail"
    ];
  };

  systemd.services.buildbuddy-work-disk-format = {
    description = "Initialize the per-VM BuildBuddy work disk once";
    before = [ "buildbuddy.mount" ];
    requires = [ "dev-disk-by\\x2did-virtio\\x2dwork.device" ];
    after = [ "dev-disk-by\\x2did-virtio\\x2dwork.device" ];
    requiredBy = [ "buildbuddy.mount" ];
    # The mount precedes local-fs.target; normal service ordering after
    # basic.target would cycle through sysinit.target back to local-fs.target.
    unitConfig.DefaultDependencies = false;
    serviceConfig.Type = "oneshot";
    script = ''
      device=/dev/disk/by-id/virtio-work
      ${pkgs.systemd}/bin/udevadm settle
      test -b "$device"
      if ${pkgs.util-linux}/bin/blkid --probe "$device" >/dev/null 2>&1; then
        type="$(${pkgs.util-linux}/bin/blkid --probe --output value --match-tag TYPE "$device")"
        if [[ "$type" != ext4 ]]; then
          echo "BuildBuddy work disk contains unexpected filesystem $type" >&2
          exit 1
        fi
      else
        status=$?
        if [[ "$status" -ne 2 ]]; then
          echo "Could not safely inspect the BuildBuddy work disk" >&2
          exit "$status"
        fi
        ${pkgs.e2fsprogs}/bin/mkfs.ext4 -F -m 0 "$device"
      fi
    '';
  };

  virtualisation.containerd = {
    enable = true;
    configFile = "${workerConfig}/containerd.toml";
  };
  virtualisation.docker = {
    enable = true;
    storageDriver = "overlay2";
    extraOptions = "--containerd=/run/containerd/containerd.sock";
    daemon.settings = builtins.fromJSON (builtins.readFile "${workerConfig}/daemon.json");
  };
  systemd.services.containerd = {
    after = [ "buildbuddy-kvm-worker-prerequisites.service" ];
    requires = [ "buildbuddy-kvm-worker-prerequisites.service" ];
    unitConfig.RequiresMountsFor = workMount;
  };
  systemd.services.docker = {
    after = [
      "buildbuddy-kvm-worker-prerequisites.service"
      "containerd.service"
    ];
    requires = [
      "buildbuddy-kvm-worker-prerequisites.service"
      "containerd.service"
    ];
    unitConfig.RequiresMountsFor = workMount;
  };

  systemd.services.buildbuddy-kvm-worker-prerequisites = {
    description = "Load and verify the nested KVM devices for the BuildBuddy worker";
    before = [
      "containerd.service"
      "docker.service"
      "buildbuddy-executor.service"
    ];
    wantedBy = [ "multi-user.target" ];
    path = [
      pkgs.coreutils
      pkgs.gnugrep
      pkgs.kmod
    ];
    serviceConfig.Type = "oneshot";
    script = ''
      if grep --quiet --word-regexp svm /proc/cpuinfo; then
        kvm_module=kvm_amd
      elif grep --quiet --word-regexp vmx /proc/cpuinfo; then
        kvm_module=kvm_intel
      else
        echo "The guest CPU does not expose SVM or VMX" >&2
        exit 1
      fi
      # systemd-modules-load tolerates missing entries; fail here if any shared
      # worker dependency or the selected vendor KVM module cannot be loaded.
      modprobe -a "$kvm_module" ${lib.escapeShellArgs guestModules}
      for module in "$kvm_module" ${lib.escapeShellArgs guestModules}; do
        if [[ ! -d "/sys/module/$module" ]]; then
          echo "Required guest module $module was not loaded" >&2
          exit 1
        fi
      done
      case "$(cat "/sys/module/$kvm_module/parameters/nested")" in
        1 | Y) ;;
        *)
          echo "Nested KVM is disabled for $kvm_module" >&2
          exit 1
          ;;
      esac
      udevadm settle
      test -c /dev/kvm
      test -c /dev/loop-control
      test -b /dev/loop0
      test -c /dev/net/tun
    '';
  };

  systemd.services.buildbuddy-kvm-worker-config = {
    description = "Prepare runtime K3s files from the mounted BuildBuddy config disk";
    before = [ "buildbuddy-executor.service" ];
    unitConfig.RequiresMountsFor = configMount;
    serviceConfig.Type = "oneshot";
    script = ''
      test -s ${configMount}/k3s.yaml
      test -s ${configMount}/registries.yaml
      test -s ${configMount}/executor.yaml
      install -d -m 0755 /run/buildbuddy-kvm-worker-k3s
      install -m 0644 ${configMount}/k3s.yaml /run/buildbuddy-kvm-worker-k3s/config.yaml
      install -m 0644 ${configMount}/registries.yaml /run/buildbuddy-kvm-worker-k3s/registries.yaml
    '';
  };

  systemd.services.buildbuddy-executor-image = {
    description = "Load the pinned BuildBuddy executor from the offline image archive";
    before = [ "buildbuddy-executor.service" ];
    after = [ "docker.service" ];
    requires = [ "docker.service" ];
    unitConfig.RequiresMountsFor = workMount;
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = executorImageLoader;
    };
  };

  systemd.services.buildbuddy-executor = {
    description = "BuildBuddy remote execution worker";
    wantedBy = [ "multi-user.target" ];
    after = [
      "network-online.target"
      "docker.service"
      "buildbuddy-executor-image.service"
      "buildbuddy-kvm-worker-config.service"
      "buildbuddy-kvm-worker-prerequisites.service"
    ];
    wants = [ "network-online.target" ];
    requires = [
      "docker.service"
      "buildbuddy-executor-image.service"
      "buildbuddy-kvm-worker-config.service"
      "buildbuddy-kvm-worker-prerequisites.service"
    ];
    unitConfig = {
      ConditionKernelCommandLine = "apparmor=0";
      RequiresMountsFor = [
        workMount
        keyMount
        configMount
      ];
    };
    environment.BUILDBUDDY_EXECUTOR_IMAGE = "${executorImageName}:${executorImageTag}";
    path = [
      pkgs.coreutils
      pkgs.docker
      pkgs.util-linux
    ];
    serviceConfig = {
      Type = "simple";
      ExecStart = executorRunner;
      ExecStop = "${pkgs.docker}/bin/docker stop --time 86595 buildbuddy-kvm-worker-executor";
      ExecStopPost = "${pkgs.coreutils}/bin/rm -f /run/buildbuddy-executor/environment";
      Restart = "always";
      RestartSec = 10;
      RuntimeDirectory = "buildbuddy-executor";
      RuntimeDirectoryMode = "0700";
      # Leave two minutes beyond the executor shutdown window for Docker to exit.
      TimeoutStopSec = "24h 5min 15s";
    };
  };

  systemd.services."serial-getty@ttyS0".serviceConfig.ExecStart = lib.mkForce [
    ""
    "${pkgs.util-linux}/bin/agetty --autologin root --noclear %I 115200,38400,9600 vt102"
  ];
}
