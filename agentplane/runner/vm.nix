# NixOS guest for the KubeVirt-backed Agentplane runner.
#
# The containerDisk supplies only this replaceable root filesystem. The Sandbox's state PVC reaches
# the guest over virtiofs at /state, laid out exactly as in a runner Pod, so a Session moves between
# kinds. The VM-local workspace disk is mounted over /state/workspaces, where Session working
# directories live in both kinds; it survives stop/start of this VM and nothing else.
#
# KubeVirt's virtiofsd runs as uid/gid 107 with no capabilities: every file the guest writes on
# /state lands as 107:107 and chown fails, so only guest root and a user whose uid and gid are both
# 107 can write there. The runner runs as root; harness children run as that 107 user.
{
  lib,
  pkgs,
  pkgsUnstable,
  agentplaneRunner,
  ...
}:
let
  configDevice = "/dev/disk/by-id/virtio-agentplane-config";
  launchDevice = "/dev/disk/by-id/virtio-agentplane-launch";
  trustDevice = "/dev/disk/by-id/virtio-agentplane-trust";
  workspaceDevice = "/dev/disk/by-id/virtio-workspace";
  workspaceDir = "/state/workspaces";
  runtimeDir = "/run/agentplane";
  sourceDir = "${runtimeDir}/config-source";
  # virtiofsd's identity (above); agentplane/runner/guest_config.py runs harnesses as it.
  agentUser = "agent";
  agentId = 107;

  # Serial console login, selected per environment by the guest config (`console_autologin`): a root
  # shell without a prompt where set, the ordinary login prompt otherwise. The guest has no account
  # passwords, so without autologin an attach stops at the prompt.
  consoleGetty = pkgs.writeShellScript "agentplane-console-getty" ''
    set -euo pipefail
    autologin=()
    if ${pkgs.jq}/bin/jq -e '.console_autologin == true' "${runtimeDir}/config.json" >/dev/null; then
      autologin=(--autologin root)
    fi
    exec ${pkgs.util-linux}/sbin/agetty "''${autologin[@]}" --keep-baud 115200,57600,38400,9600 ttyS0 "''${TERM:-vt220}"
  '';

  assertBlankDevice = pkgs.writeTextFile {
    name = "agentplane-assert-blank-device";
    executable = true;
    text = "#!${pkgs.python3}/bin/python3\n" + builtins.readFile ./assert_blank_device.py;
  };

  workspacePreparation = pkgs.writeShellScript "agentplane-prepare-workspace" ''
    set -euo pipefail
    device="${workspaceDevice}"
    for _ in $(seq 1 60); do
      [ -b "$device" ] && break
      sleep 1
    done
    if [ ! -b "$device" ]; then
      echo "KubeVirt workspace disk did not appear at $device" >&2
      exit 1
    fi
    if type="$(blkid -p -s TYPE -o value "$device" 2>/dev/null)"; then
      label="$(blkid -p -s LABEL -o value "$device" 2>/dev/null || true)"
      signatures="$(wipefs --no-act --noheadings --output TYPE "$device")"
      if [ "$type" != ext4 ] || [ "$label" != APWORKSPACE ] || [ "$signatures" != ext4 ]; then
        echo "Refusing unexpected filesystem on the workspace disk ($type, label $label)" >&2
        exit 1
      fi
      exit 0
    else
      status=$?
      if [ "$status" -ne 2 ]; then
        echo "Could not inspect the workspace disk (blkid status $status)" >&2
        exit 1
      fi
    fi
    if [ -n "$(wipefs --no-act --noheadings "$device")" ]; then
      echo "Refusing unrecognized disk signatures on the workspace disk" >&2
      exit 1
    fi
    # No known signature is not proof that the disk is empty. A CDI blank DataVolume is
    # zero-filled, so only a disk that reads as zeros end to end is formatted; one that lost its
    # filesystem but still holds data is left for a person to look at.
    if ${assertBlankDevice} "$device"; then
      :
    else
      status=$?
      # 3 is NOT_BLANK_EXIT_CODE in assert_blank_device.py.
      if [ "$status" -eq 3 ]; then
        echo "Refusing to format a non-zero workspace disk without a recognized filesystem" >&2
      else
        echo "Could not verify the blank workspace disk (status $status)" >&2
      fi
      exit 1
    fi
    mkfs.ext4 -F -L APWORKSPACE "$device"
  '';
in
{
  imports = [ ./../../nix/nixos/modules/vm-hardware.nix ];

  networking.hostName = "agentplane-runner-vm";
  services.openssh.enable = lib.mkForce false;
  services.qemuGuest.enable = true;

  # KubeVirt's masquerade hands the guest its address alone; probing it with ARP first holds the
  # network, and the runner's first dial, back by about five seconds.
  networking.dhcpcd.extraConfig = "noarp";

  # Keep the replaceable guest root small: the runner's journals live on the state PVC and agent
  # work on the workspace disk.
  virtualisation.diskSize = 8 * 1024;
  boot.kernelModules = [ "virtiofs" ];
  boot.kernelParams = [
    "console=ttyS0,115200n8"
    # The root disk is discarded at every stop and the workspace is a journalled ext4, so a boot-time
    # check has nothing to repair; it costs seconds of seek-bound reads on a cold node.
    "fsck.mode=skip"
  ];
  # Nobody is at the boot menu; its countdown is the first seconds of every start.
  boot.loader.timeout = 0;
  # The runner's journal (a start failure's traceback included) reaches the serial console, which
  # the VMI serial log and `virtctl console` read, without a guest exec.
  services.journald.console = "/dev/ttyS0";

  systemd.services."serial-getty@ttyS0" = {
    overrideStrategy = "asDropin";
    after = [ "agentplane-config.service" ];
    wants = [ "agentplane-config.service" ];
    serviceConfig.ExecStart = [
      ""
      "${consoleGetty}"
    ];
  };

  users.groups.${agentUser}.gid = agentId;
  users.users.${agentUser} = {
    isSystemUser = true;
    uid = agentId;
    group = agentUser;
    home = "${workspaceDir}/.home";
    shell = pkgs.bash;
  };
  users.mutableUsers = false;
  # The guest accepts no connections: KubeVirt's serial console and the runner's outbound control
  # stream are its only interfaces, so do not create an account password or an SSH key just to
  # satisfy host defaults.
  users.allowNoPasswordLogin = true;

  # The KubeVirt filesystem named `state` is the virtiofs tag.
  fileSystems."/state" = {
    device = "state";
    fsType = "virtiofs";
    noCheck = true;
    options = [
      "noatime"
      "nodev"
      "nosuid"
    ];
  };
  fileSystems.${workspaceDir} = {
    device = workspaceDevice;
    fsType = "ext4";
    options = [
      "noatime"
      "nodev"
      "nosuid"
    ];
    autoFormat = false;
    autoResize = false;
  };

  systemd.services.agentplane-config = {
    description = "Read the Agentplane public configuration, launch and trust disks";
    wantedBy = [ "local-fs.target" ];
    before = [
      "agentplane-runner.service"
      "local-fs.target"
    ];
    after = [
      "systemd-remount-fs.service"
      "systemd-udev-trigger.service"
    ];
    path = [
      pkgs.coreutils
      pkgs.util-linux
    ];
    unitConfig.DefaultDependencies = false;
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      UMask = "0022";
    };
    script = ''
      set -euo pipefail
      install -d -m0755 "${runtimeDir}" "${sourceDir}"
      cleanup() {
        if mountpoint -q "${sourceDir}"; then umount "${sourceDir}"; fi
      }
      trap cleanup EXIT
      copy_disk() {
        local device="$1" mounted=0
        shift
        for _ in $(seq 1 60); do
          if [ -b "$device" ] && mount -o ro,nosuid,nodev,noexec "$device" "${sourceDir}" 2>/dev/null; then
            mounted=1
            break
          fi
          sleep 1
        done
        if [ "$mounted" -ne 1 ]; then
          echo "KubeVirt read-only Agentplane disk missing at $device" >&2
          return 1
        fi
        for file in "$@"; do
          test -s "${sourceDir}/$file" || { echo "Agentplane disk is missing $file" >&2; return 1; }
          install -m0644 "${sourceDir}/$file" "${runtimeDir}/$file"
        done
        umount "${sourceDir}"
      }
      copy_disk "${configDevice}" config.json kubeconfig
      copy_disk "${launchDevice}" launch.json
      copy_disk "${trustDevice}" ca-certificates.crt ca-certificates.p12
      rmdir "${sourceDir}"
      trap - EXIT
    '';
  };

  systemd.services.agentplane-workspace-prepare = {
    description = "Format the Agentplane workspace disk when it is blank";
    after = [ "systemd-udev-trigger.service" ];
    before = [
      "state-workspaces.mount"
      "local-fs.target"
    ];
    requiredBy = [ "state-workspaces.mount" ];
    unitConfig.DefaultDependencies = false;
    path = [
      pkgs.coreutils
      pkgs.e2fsprogs
      pkgs.util-linux
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = "${workspacePreparation}";
  };

  systemd.services.agentplane-workspace = {
    description = "Give the harness user the Agentplane workspace";
    requires = [ "state-workspaces.mount" ];
    after = [ "state-workspaces.mount" ];
    before = [ "agentplane-runner.service" ];
    path = [
      pkgs.acl
      pkgs.coreutils
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    # The runner, as root, creates each Session's working directory; the default ACL hands every
    # new entry to the harness user. /state itself needs nothing: virtiofs makes everything 107.
    script = ''
      set -euo pipefail
      chown ${agentUser}:${agentUser} ${workspaceDir}
      chmod 2770 ${workspaceDir}
      setfacl -m d:u:${agentUser}:rwx,d:g:${agentUser}:rwx ${workspaceDir}
      install -d -m0700 -o ${agentUser} -g ${agentUser} ${workspaceDir}/.home
    '';
  };

  systemd.services.agentplane-runner = {
    description = "Agentplane native harness runner";
    wantedBy = [ "multi-user.target" ];
    requires = [
      "agentplane-config.service"
      "agentplane-workspace.service"
    ];
    after = [
      "agentplane-config.service"
      "agentplane-workspace.service"
      "network.target"
    ];
    unitConfig.RequiresMountsFor = [
      "/state"
      workspaceDir
    ];
    path = [
      agentplaneRunner
      pkgs.coreutils
      pkgs.systemd
    ];
    environment = {
      ANTHROPIC_AUTH_TOKEN = "agentplane-credential-agentplane-workload";
      OPENAI_API_KEY = "agentplane-credential-agentplane-workload";
    };
    serviceConfig = {
      Type = "simple";
      ExecStart = "${agentplaneRunner}/bin/agentplane-runner-vm";
      UMask = "0007";
      Restart = "on-failure";
      RestartSec = "2s";
      TimeoutStopSec = "25s";
      Delegate = "cpu io memory pids";
      DelegateSubgroup = "runner";
      MemoryHigh = "6G";
      MemoryMax = "7G";
      CPUQuota = "700%";
      TasksMax = 1024;
      # Only signal the runner first so its shutdown drains native processes while their inherited
      # state-owner descriptor remains open. systemd escalates to the whole unit after 25 seconds.
      KillMode = "mixed";
      # Root with only what it uses: entering harnesses as the agent user and stopping them, and
      # reading and writing the agent-owned files on /state and the workspace.
      CapabilityBoundingSet = [
        "CAP_DAC_OVERRIDE"
        "CAP_FOWNER"
        "CAP_KILL"
        "CAP_SETGID"
        "CAP_SETUID"
      ];
      NoNewPrivileges = true;
      LockPersonality = true;
      ProtectKernelModules = true;
      ProtectKernelLogs = true;
      PrivateTmp = true;
      ProtectSystem = "strict";
      ReadWritePaths = [
        "/state"
        workspaceDir
      ];
      TemporaryFileSystem = [ "/tmp:rw,size=1G,nr_inodes=100000,mode=1777" ];
    };
  };

  environment.systemPackages = with pkgs; [
    bashInteractive
    coreutils
    curl
    diffutils
    e2fsprogs
    file
    findutils
    gawk
    git
    gnugrep
    gnupatch
    gnused
    gnutar
    gzip
    jq
    kubectl
    less
    openssl
    procps
    python3
    ripgrep
    unzip
    util-linux
    which
    xz
    pkgsUnstable.claude-code
    pkgsUnstable.codex
  ];

  programs.nix-ld.enable = true;
  programs.nix-ld.libraries = with pkgs; [
    stdenv.cc.cc.lib
    zlib
    glibc
    openssl.out
  ];

  system.stateVersion = "25.11";
}
