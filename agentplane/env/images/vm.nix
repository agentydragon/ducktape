# NixOS guest for the KubeVirt-backed Agentplane runner.
#
# The containerDisk supplies only this replaceable root filesystem. The Sandbox's state PVC reaches
# the guest over virtiofs at /state, laid out exactly as in a runner Pod, so a Session moves between
# kinds. The workspace disk is mounted over /state/workspaces, where Session working directories
# live in both kinds; its claim belongs to the Sandbox and survives stop/start.
#
# KubeVirt's virtiofsd runs as uid/gid 107 with no capabilities: every file the guest writes on
# /state lands as 107:107 and chown fails, so only guest root and a user whose uid and gid are both
# 107 can write there. The runner runs as root; harness children run as that 107 user.
#
# Runtime configuration, launch data and egress trust arrive on read-only ConfigMap disks. Runtime
# TLS credentials and its trust bundle arrive on read-only virtiofs mounts.
#
# Runtime, Runtime, Execution service and the `agentplane` command are not in this closure.
# They arrive on the read-only runtime disk (//agentplane/env/images:disk_image), a second
# containerDisk mounted at /run/agentplane/environment, so a service release republishes that small
# disk and not this guest. The harnesses and tools stay here.
{
  config,
  lib,
  pkgs,
  pkgsUnstable,
  preCommitPackages,
  firecrackerGuest ? false,
  ...
}:
let
  guestContract = builtins.fromJSON (builtins.readFile ./vm_guest_contract.json);
  firecrackerContract = builtins.fromJSON (builtins.readFile ./firecracker_guest_contract.json);
  stateDisk = if firecrackerGuest then firecrackerContract.disks.state else guestContract.state;
  workspaceDisk =
    if firecrackerGuest then firecrackerContract.disks.workspace else guestContract.disks.workspace;
  runtimeDisk =
    if firecrackerGuest then firecrackerContract.disks.runtime else guestContract.disks.runtime;
  configDisk =
    if firecrackerGuest then firecrackerContract.disks.config else guestContract.disks.config;
  launchDisk =
    if firecrackerGuest then firecrackerContract.disks.launch else guestContract.disks.launch;
  trustDisk =
    if firecrackerGuest then
      firecrackerContract.disks.egress_trust
    else
      guestContract.disks.egress_trust;
  stateDir = stateDisk.mount;
  workspaceDir = workspaceDisk.mount;
  runtimeDir = "/run/agentplane";
  environmentDir = runtimeDisk.mount;
  configDir = configDisk.mount;
  runtimeConfigFile = "${configDir}/${guestContract.files.runtime_config}";
  runtimeTLS = guestContract.environment_runtime_tls;
  runtimeTrust = guestContract.environment_runtime_trust;
  runtimeWorkloadTrust = guestContract.environment_runtime_workload_trust;
  # ConfigMap-backed disks carry immutable Runtime configuration, launch identity and egress trust.
  kubeVirtSeedDisks = {
    config = guestContract.disks.config;
    launch = guestContract.disks.launch;
    trust = guestContract.disks.egress_trust;
  };
  diskDevice =
    disk: if firecrackerGuest then disk.device else "/dev/disk/by-id/virtio-${disk.serial}";
  stateDevice = if firecrackerGuest then stateDisk.device else stateDisk.volume_name;
  workspaceDevice = workspaceDisk.device;
  readOnlyFilesystem = disk: {
    device = diskDevice disk;
    fsType = disk.filesystem;
    noCheck = true;
    options = [
      "ro"
      "nosuid"
      "nodev"
      "noexec"
    ];
  };
  fileSystemMatches =
    path: device: fsType:
    builtins.hasAttr path config.fileSystems
    && config.fileSystems.${path}.device == device
    && config.fileSystems.${path}.fsType == fsType;
  guestMountsMatchContract =
    fileSystemMatches stateDir stateDevice stateDisk.filesystem
    && fileSystemMatches workspaceDir workspaceDevice workspaceDisk.filesystem
    && fileSystemMatches environmentDir (diskDevice runtimeDisk) runtimeDisk.filesystem
    && fileSystemMatches runtimeTLS.private_mount runtimeTLS.volume_name runtimeTLS.filesystem
    && fileSystemMatches runtimeTrust.mount runtimeTrust.volume_name runtimeTrust.filesystem
    &&
      fileSystemMatches runtimeWorkloadTrust.mount runtimeWorkloadTrust.volume_name
        runtimeWorkloadTrust.filesystem
    && lib.all (disk: fileSystemMatches disk.mount (diskDevice disk) disk.filesystem) (
      builtins.attrValues kubeVirtSeedDisks
    )
    && lib.elem "d ${builtins.dirOf runtimeTLS.private_mount} 0700 root root -" config.systemd.tmpfiles.rules
    && lib.elem "L+ ${runtimeTLS.runtime_path} - - - - ${runtimeTLS.private_mount}" config.systemd.tmpfiles.rules;
  firecrackerMountsMatchContract =
    lib.all (disk: fileSystemMatches disk.mount disk.device disk.filesystem) (
      builtins.attrValues firecrackerContract.disks
    )
    && !(builtins.hasAttr runtimeTLS.private_mount config.fileSystems)
    && !(builtins.hasAttr runtimeTrust.mount config.fileSystems)
    && lib.elem "d ${builtins.dirOf runtimeTLS.private_mount} 0700 root root -" config.systemd.tmpfiles.rules
    && lib.elem "d ${runtimeTLS.private_mount} 0700 root root -" config.systemd.tmpfiles.rules
    && lib.elem "d ${runtimeTrust.mount} 0755 root root -" config.systemd.tmpfiles.rules
    && lib.elem "L+ ${runtimeTLS.runtime_path} - - - - ${runtimeTLS.private_mount}" config.systemd.tmpfiles.rules;
  # virtiofsd's identity (above); the Go Runtime runs harnesses as this user.
  agentUser = "agent";
  agentId = 107;

  codexWithoutVoice = import ./codex/package.nix { inherit pkgs pkgsUnstable; };

  # An ExecCondition that passes where the environment config's `environment.console_autologin` (unset is false)
  # equals `autologin`. jq exits 1 otherwise, which skips the unit without failing it; an unreadable
  # config (exit 2 or 5) skips both console units. It prints nothing, which the console, a tty
  # for these units, would show.
  consoleAutologinIs =
    autologin:
    "${pkgs.jq}/bin/jq 'if (.environment.kind == \"vm\" and .environment.console_autologin == true) == ${lib.boolToString autologin} then empty else (\"\" | halt_error(1)) end' ${runtimeConfigFile}";
  applyRunnerResourceLimits = pkgs.writeShellScript "agentplane-runner-resource-limits" (
    builtins.readFile ./systemd/apply-resource-limits
  );
  prepareHome = pkgs.writeShellScript "agentplane-home-prepare" (builtins.readFile ./home_prepare);
  supervisionUnits = pkgs.linkFarm "agentplane-supervision-units" (
    map
      (name: {
        name = "lib/systemd/system/${name}";
        path = ./systemd + "/${name}";
      })
      [
        "agentplane-environment-runtime.service"
        "agentplane.slice"
        "agentplane-workloads.slice"
        "agentplane-executors.slice"
      ]
  );
in
{
  assertions = [
    {
      assertion = if firecrackerGuest then firecrackerMountsMatchContract else guestMountsMatchContract;
      message = "Agentplane VM guest mounts and private TLS path must match vm_guest_contract.json";
    }
  ];

  imports = lib.optionals (!firecrackerGuest) [ ./../../../nix/nixos/modules/vm-hardware.nix ];

  networking.hostName = lib.mkIf (!firecrackerGuest) "agentplane-environment-vm";
  # The Runner Gateway initiates mTLS gRPC connections to the guest's runner listener.
  networking.firewall.allowedTCPPorts = [ 9090 ];
  services.openssh.enable = lib.mkForce false;
  services.qemuGuest.enable = lib.mkIf firecrackerGuest (lib.mkForce false);
  systemd.packages = [ supervisionUnits ];
  # Native services share exec-server sockets and temporary files with their harnesses.
  boot.tmp.useTmpfs = true;
  boot.tmp.tmpfsSize = "1G";

  # KubeVirt's masquerade hands the guest its address alone; probing it with ARP first holds the
  # network, and the runner's first dial, back by about five seconds.
  networking.dhcpcd.extraConfig = "noarp";

  # The guest cannot reach NTP (UDP/123): timesyncd retried the pool for the whole boot and filled
  # the serial console. The VM starts on the host's clock and is never suspended.
  services.timesyncd.enable = false;

  # Keep the replaceable guest root small: the runner's journals live on the state PVC and agent
  # work on the workspace disk. Every closure byte is pulled to the node before the guest boots.
  virtualisation.diskSize = 8 * 1024;
  # Nobody reads man pages, info or the NixOS manual in a guest whose only interface is the runner.
  documentation.enable = false;
  # disk-image.nix cannot pass copyChannel, whose default puts a second nixpkgs source in the image.
  image.modules.qemu-efi =
    { config, pkgs, ... }:
    lib.mkIf (!firecrackerGuest) {
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
  boot.kernelModules = lib.optionals (!firecrackerGuest) [ "virtiofs" ];
  boot.kernelParams = [
    "console=ttyS0,115200n8"
    # The root disk is discarded at every stop and the workspace is a journalled ext4, so a boot-time
    # check has nothing to repair; it costs seconds of seek-bound reads on a cold node.
    "fsck.mode=skip"
  ]
  ++ lib.optionals firecrackerGuest [
    "root=${firecrackerContract.disks.root.device}"
    "rootfstype=${firecrackerContract.disks.root.filesystem}"
    "rootwait"
    "rw"
  ];
  # Nobody is at the boot menu; its countdown is the first seconds of every start.
  boot.loader.timeout = 0;
  # The runner's journal (a start failure's traceback included) reaches the serial console, which
  # the VMI serial log and `virtctl console` read, without a guest exec.
  services.journald.console = "/dev/ttyS0";

  # Serial console login, selected per environment by the Runtime config's `environment.console_autologin`: the
  # ordinary login prompt where unset, a root shell without a prompt where set. The guest has no
  # account passwords, so without autologin an attach stops at the prompt.
  #
  # Set on the template, merged into NixOS's own drop-in: NixOS names every drop-in `overrides.conf`
  # and an instance's drop-in hides the template's of the same name, so a `serial-getty@ttyS0` one
  # drops NixOS's agetty for upstream's absent `/usr/bin/agetty`, which crash-loops on the console.
  # ttyS0 is the guest's only serial getty.
  systemd.services."serial-getty@" = {
    unitConfig.RequiresMountsFor = [ "${runtimeDir}/config" ];
    serviceConfig.ExecCondition = consoleAutologinIs false;
  };
  # serial-getty@.service for ttyS0 with agetty's autologin.
  systemd.services.agentplane-console-autologin = {
    description = "Serial console root shell on ttyS0";
    wantedBy = [ "getty.target" ];
    before = [ "getty.target" ];
    bindsTo = [ "dev-ttyS0.device" ];
    after = [
      "dev-ttyS0.device"
      "systemd-user-sessions.service"
      "getty-pre.target"
    ];
    unitConfig = {
      IgnoreOnIsolate = true;
      RequiresMountsFor = [ "${runtimeDir}/config" ];
    };
    serviceConfig = {
      ExecCondition = consoleAutologinIs true;
      ExecStart = "${pkgs.util-linux}/sbin/agetty --login-program ${pkgs.shadow}/bin/login --autologin root --keep-baud 115200,57600,38400,9600 ttyS0 $TERM";
      Type = "idle";
      Restart = "always";
      UtmpIdentifier = "ttyS0";
      StandardInput = "tty";
      StandardOutput = "tty";
      TTYPath = "/dev/ttyS0";
      TTYReset = true;
      TTYVHangup = true;
      IgnoreSIGPIPE = false;
      SendSIGHUP = true;
    };
  };

  users.groups.${agentUser}.gid = agentId;
  users.users.${agentUser} = {
    isSystemUser = true;
    uid = agentId;
    group = agentUser;
    home = "${stateDir}/home";
    shell = pkgs.bash;
  };
  users.mutableUsers = false;
  # Interactive access uses KubeVirt's serial console, so do not create an account password or
  # an SSH key just to satisfy host defaults.
  users.allowNoPasswordLogin = true;

  fileSystems =
    if firecrackerGuest then
      {
        ${firecrackerContract.disks.root.mount} = {
          device = firecrackerContract.disks.root.device;
          fsType = firecrackerContract.disks.root.filesystem;
          noCheck = true;
          options = [ "noatime" ];
        };
        ${stateDir} = {
          device = stateDevice;
          fsType = stateDisk.filesystem;
          noCheck = true;
          options = [
            "noatime"
            "nodev"
            "nosuid"
          ];
        };
        ${workspaceDir} = {
          device = workspaceDevice;
          fsType = workspaceDisk.filesystem;
          noCheck = true;
          options = [
            "noatime"
            "nodev"
            "nosuid"
          ];
        };
        ${environmentDir} = {
          device = diskDevice runtimeDisk;
          fsType = runtimeDisk.filesystem;
          noCheck = true;
          options = [
            "ro"
            "nosuid"
            "nodev"
          ];
        };
        ${configDisk.mount} = readOnlyFilesystem configDisk;
        ${launchDisk.mount} = readOnlyFilesystem launchDisk;
        ${trustDisk.mount} = readOnlyFilesystem trustDisk;
      }
    else
      {
        # The KubeVirt filesystem named `state` is the virtiofs tag.
        ${stateDir} = {
          device = stateDevice;
          fsType = stateDisk.filesystem;
          noCheck = true;
          options = [
            "noatime"
            "nodev"
            "nosuid"
          ];
        };
        ${workspaceDir} = {
          device = workspaceDevice;
          fsType = workspaceDisk.filesystem;
          options = [
            "noatime"
            "nodev"
            "nosuid"
          ];
          autoFormat = true;
          autoResize = false;
        };
        # The Runtime disk is an erofs image on the containerDisk of serial `agentplane-runner`. Unlike
        # the seed disks below it is executable. `nofail` leaves a missing disk to the runner, which
        # requires its mount.
        ${environmentDir} = {
          device = diskDevice runtimeDisk;
          fsType = runtimeDisk.filesystem;
          noCheck = true;
          options = [
            "ro"
            "nosuid"
            "nodev"
            "nofail"
          ];
        };
        # KubeVirt Secret virtiofs mounts do not expose defaultMode, so keep the TLS key behind a
        # root-only parent directory. The runner runs as root; harnesses run as the unprivileged agent.
        ${runtimeTLS.private_mount} = {
          device = runtimeTLS.volume_name;
          fsType = runtimeTLS.filesystem;
          noCheck = true;
          options = [
            "ro"
            "nosuid"
            "nodev"
            "noexec"
            "nofail"
            "x-systemd.requires=systemd-tmpfiles-setup.service"
          ];
        };
        ${runtimeTrust.mount} = {
          device = runtimeTrust.volume_name;
          fsType = runtimeTrust.filesystem;
          noCheck = true;
          options = [
            "ro"
            "nosuid"
            "nodev"
            "noexec"
            "nofail"
            "x-systemd.requires=systemd-tmpfiles-setup.service"
          ];
        };
        # The workload CA alone, which verifies the Gateway's client certificate. `nofail` also
        # covers a Sandbox Controller that does not attach it yet.
        ${runtimeWorkloadTrust.mount} = {
          device = runtimeWorkloadTrust.volume_name;
          fsType = runtimeWorkloadTrust.filesystem;
          noCheck = true;
          options = [
            "ro"
            "nosuid"
            "nodev"
            "noexec"
            "nofail"
            "x-systemd.requires=systemd-tmpfiles-setup.service"
          ];
        };
      }
      // lib.mapAttrs' (
        _: disk:
        lib.nameValuePair disk.mount {
          device = diskDevice disk;
          fsType = disk.filesystem;
          noCheck = true;
          options = [
            "ro"
            "nosuid"
            "nodev"
            "noexec"
            "nofail"
          ];
        }
      ) kubeVirtSeedDisks;

  # Ownership, the setgid mode and inherited harness access are persistent tmpfiles policy rather
  # than imperative chown/chmod/setfacl commands. The service below reapplies it after the mount.
  systemd.tmpfiles.rules = [
    # Runtime journals stay root-owned; the shared Codex process runs as agent.
    "d ${stateDir}/codex 0700 ${agentUser} ${agentUser} -"
    "d ${workspaceDir} 2770 ${agentUser} ${agentUser} -"
    "z ${workspaceDir} 2770 ${agentUser} ${agentUser} -"
    "a+ ${workspaceDir} - - - - d:user:${toString agentId}:rwx,d:group:${toString agentId}:rwx"
    "d ${runtimeDir} 0755 root root -"
    "d ${builtins.dirOf runtimeTLS.private_mount} 0700 root root -"
    "d ${runtimeTLS.private_mount} 0700 root root -"
    "d ${runtimeTrust.mount} 0755 root root -"
    "d ${runtimeWorkloadTrust.mount} 0755 root root -"
    "d ${runtimeDir}/execution-service 0700 root root -"
    "L+ ${runtimeTLS.runtime_path} - - - - ${runtimeTLS.private_mount}"
  ];

  systemd.services.agentplane-workspace = {
    description = "Give the harness user the Agentplane workspace";
    requires = [
      "state.mount"
      "state-workspaces.mount"
    ];
    after = [
      "state.mount"
      "state-workspaces.mount"
    ];
    before = [ "agentplane-home-prepare.service" ];
    path = [
      pkgs.acl
      pkgs.coreutils
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    # Reapply declarative directory and ACL rules only after the persistent workspace is mounted.
    serviceConfig.ExecStart = "${pkgs.systemd}/bin/systemd-tmpfiles --create --prefix=${workspaceDir}";
  };

  systemd.services.agentplane-home-prepare = {
    description = "Move the VM home onto the persistent state volume";
    requires = [
      "state.mount"
      "state-workspaces.mount"
      "agentplane-workspace.service"
    ];
    after = [
      "state.mount"
      "state-workspaces.mount"
      "agentplane-workspace.service"
    ];
    before = [ "agentplane-environment-runtime.service" ];
    path = [ pkgs.coreutils ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      User = agentUser;
      Group = agentUser;
      ExecStart = "${prepareHome} ${workspaceDir}/.home ${stateDir}/home";
    };
  };

  systemd.services.agentplane-firecracker-thaw = lib.mkIf firecrackerGuest {
    description = "Set guest time and Runtime credentials before enabling guest networking";
    wantedBy = [ "network-pre.target" ];
    before = [ "network-pre.target" ];
    after = [
      "local-fs.target"
      "systemd-tmpfiles-setup.service"
    ];
    unitConfig.RequiresMountsFor = [
      environmentDir
    ];
    serviceConfig = {
      Type = "notify";
      NotifyAccess = "main";
      ExecStart = "${environmentDir}/bin/agentplane-firecracker-thaw-agent";
      Restart = "on-failure";
      RestartSec = 1;
      User = "root";
      UMask = "0077";
      CapabilityBoundingSet = "CAP_SYS_TIME CAP_NET_BIND_SERVICE";
      AmbientCapabilities = "CAP_SYS_TIME CAP_NET_BIND_SERVICE";
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = "read-only";
      ReadWritePaths = [ runtimeTLS.private_mount ];
      RestrictAddressFamilies = [
        "AF_UNIX"
        "AF_VSOCK"
      ];
      LockPersonality = true;
    };
  };

  # The production guest and the real-manager test fixture consume the same Runtime unit policy.
  # Only the Nix store helper path and workspace preparation dependency need a drop-in.
  systemd.services.agentplane-environment-runtime = {
    overrideStrategy = "asDropin";
    wantedBy = [ "multi-user.target" ];
    requires = [
      "agentplane-home-prepare.service"
    ]
    ++ lib.optionals firecrackerGuest [ "agentplane-firecracker-thaw.service" ];
    bindsTo = lib.optionals firecrackerGuest [ "agentplane-firecracker-thaw.service" ];
    after = [
      "agentplane-home-prepare.service"
    ]
    ++ lib.optionals firecrackerGuest [ "agentplane-firecracker-thaw.service" ];
    path = [
      pkgs.coreutils
      pkgs.systemd
    ];
    serviceConfig.ExecStartPre = [
      ""
      "${applyRunnerResourceLimits}"
    ];
  };

  # The guest's package set: the tools every Sandbox kind shares (<./tools.nix>), the same
  # hook/build tools as runner-ducktape, what the guest needs of its own, and the harnesses.
  environment.systemPackages =
    import ./tools.nix pkgs
    ++ preCommitPackages
    ++ (with pkgs; [
      bashInteractive
      e2fsprogs
      util-linux
      pkgsUnstable.claude-code
      codexWithoutVoice
    ]);

  programs.nix-ld.enable = true;
  programs.nix-ld.libraries =
    (import ../../../nix/lib/nix-ld-image.nix { inherit pkgs; }).runtimeLibPkgs;

  system.stateVersion = "25.11";
}
