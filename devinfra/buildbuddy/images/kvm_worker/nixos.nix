# Static NixOS guest for the BuildBuddy KVM worker.
{
  lib,
  pkgs,
  ...
}:
let
  executorImage = "buildbuddy.bbcr.io/public/buildbuddy-executor-enterprise@sha256:465d670159f7f2be756eb31815d7c6be54ce812de74a5cc49d3f6bda1fc7b078";
  executorConfig = pkgs.writeText "buildbuddy-kvm-worker-executor.yaml" ''
    docker_cap_add: SYS_ADMIN,NET_ADMIN,MKNOD
    executor:
      app_target: grpcs://remote.buildbuddy.io:443
      api_key: "''${BUILDBUDDY_API_KEY}"
      millicpu: 8000
      memory_bytes: 23622320128
      max_concurrent_tasks: 1
      root_directory: /buildbuddy/remotebuilds
      local_cache_directory: /buildbuddy/filecache
      local_cache_size_bytes: 40000000000
      docker_socket: /var/run/docker.sock
      default_isolation_type: docker
      enable_bare_runner: false
      enable_firecracker: false
      enable_oci: false
      enable_podman: false
      docker_inherit_user_ids: true
      docker_devices:
        - path_on_host: /dev
          path_in_container: /dev
          cgroup_permissions: rwm
      container_image_name_rewrites:
        - prefix: ghcr.io/
          replacement: oci-cache.oci-cache.svc.cluster.local/ghcr/
  '';
  docker = "${pkgs.docker_29}/bin/docker";
  prepareExecutorEnvironment = pkgs.writeShellScript "prepare-buildbuddy-kvm-worker-environment" ''
    set -eu
    key_file=/run/buildbuddy-key/api-key
    test -s "$key_file"
    install -d -m 0700 /run/buildbuddy-executor
    umask 077
    api_key="$(cat "$key_file")"
    test -n "$api_key"
    printf 'BUILDBUDDY_API_KEY=%s\n' "$api_key" > /run/buildbuddy-executor/environment
  '';
  runExecutor = pkgs.writeShellScript "run-buildbuddy-kvm-worker-executor" ''
    set -eu
    if ${docker} container inspect buildbuddy-kvm-worker-executor >/dev/null 2>&1; then
      ${docker} rm -f buildbuddy-kvm-worker-executor
    fi
    exec ${docker} run \
      --rm \
      --name buildbuddy-kvm-worker-executor \
      --pull=missing \
      --network host \
      --env MY_HOSTNAME=buildbuddy-kvm-worker \
      --env MY_POOL=wyrm2-kvm-vm \
      --env-file /run/buildbuddy-executor/environment \
      --volume ${executorConfig}:/config.yaml:ro \
      --volume /var/run/docker.sock:/var/run/docker.sock \
      --volume /buildbuddy:/buildbuddy \
      ${executorImage} \
      --config_file=/config.yaml \
      --server_type=buildbuddy-executor
  '';
in
{
  networking.hostName = "buildbuddy-kvm-worker";
  networking.firewall.enable = false;
  services.openssh.enable = lib.mkForce false;
  services.timesyncd.enable = false;
  services.getty.autologinUser = "root";
  documentation.enable = false;

  # Keep the replaceable EFI root disk small; action state and the Docker store live
  # on the separate persistent KubeVirt emptyDisk.
  virtualisation.diskSize = 8 * 1024;
  image.modules.qemu-efi =
    {
      config,
      lib,
      pkgs,
      ...
    }:
    {
      system.build.image = lib.mkForce (
        import (pkgs.path + "/nixos/lib/make-disk-image.nix") {
          inherit lib config pkgs;
          inherit (config.virtualisation) diskSize;
          inherit (config.image) baseName format;
          partitionTableType = "efi";
          copyChannel = false;
        }
      );
    };

  fileSystems."/buildbuddy" = {
    device = "/dev/disk/by-id/virtio-work";
    fsType = "ext4";
    autoFormat = true;
    options = [ "noatime" ];
  };
  fileSystems."/run/buildbuddy-key" = {
    device = "/dev/disk/by-id/virtio-bbkey";
    fsType = "iso9660";
    options = [ "ro" ];
  };

  boot.kernelModules = [
    "br_netfilter"
    "bridge"
    "ip6_tables"
    "ip6table_nat"
    "ip_tables"
    "iptable_nat"
    "kvm-amd"
    "loop"
    "nf_conntrack"
    "nf_nat"
    "overlay"
    "tun"
    "vhost_net"
    "vhost_vsock"
    "vxlan"
  ];
  boot.extraModprobeConfig = ''
    options kvm_amd nested=1
    options loop max_loop=64
  '';
  boot.kernel.sysctl = {
    "net.bridge.bridge-nf-call-iptables" = 1;
    "net.bridge.bridge-nf-call-ip6tables" = 1;
    "net.ipv4.ip_forward" = 1;
  };
  boot.kernelParams = [ "console=ttyS0,115200n8" ];
  services.journald.console = "/dev/ttyS0";

  # Docker actions may need SYS_ADMIN for nested KubeVirt mounts. The guest has no
  # outer host devices; its /dev is the only device tree exposed to those actions.
  security.apparmor.enable = false;
  virtualisation.docker.enable = true;
  virtualisation.docker.package = pkgs.docker_29;
  virtualisation.docker.daemon.settings = {
    "data-root" = "/buildbuddy/docker";
    "insecure-registries" = [ "oci-cache.oci-cache.svc.cluster.local" ];
  };
  systemd.services.docker.unitConfig.RequiresMountsFor = [ "/buildbuddy" ];

  systemd.services.buildbuddy-executor = {
    description = "BuildBuddy remote execution worker";
    wantedBy = [ "multi-user.target" ];
    requires = [ "docker.service" ];
    after = [ "docker.service" ];
    unitConfig.RequiresMountsFor = [
      "/buildbuddy"
      "/run/buildbuddy-key"
    ];
    serviceConfig = {
      Type = "simple";
      ExecStartPre = prepareExecutorEnvironment;
      ExecStart = runExecutor;
      ExecStop = "${docker} stop --time 30 buildbuddy-kvm-worker-executor";
      ExecStopPost = "${pkgs.coreutils}/bin/rm -f /run/buildbuddy-executor/environment";
      Restart = "always";
      RestartSec = 10;
      TimeoutStopSec = 45;
    };
  };

  system.stateVersion = "25.11";
}
