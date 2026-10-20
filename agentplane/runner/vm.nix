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
# The runner's configuration, launch file and CA bundle arrive on three read-only KubeVirt ConfigMap
# disks, mounted under /run/agentplane and linked to the paths the runner reads.
{
  lib,
  pkgs,
  pkgsUnstable,
  agentplaneRunner,
  agentplaneCli,
  ...
}:
let
  workspaceDevice = "/dev/disk/by-id/virtio-workspace";
  workspaceDir = "/state/workspaces";
  runtimeDir = "/run/agentplane";
  # The Go runner reads these files from runtimeDir. Each lives on
  # the KubeVirt ConfigMap disk of serial `agentplane-<disk>`, mounted at `${runtimeDir}/<disk>`.
  seedDiskOf = {
    "config.json" = "config";
    "kubeconfig" = "config";
    "launch.json" = "launch";
    "ca-certificates.crt" = "trust";
    "ca-certificates.p12" = "trust";
  };
  seedDisks = lib.unique (lib.attrValues seedDiskOf);
  # virtiofsd's identity (above); the Go runner runs harnesses as this user.
  agentUser = "agent";
  agentId = 107;

  # nixpkgs' codex bundles a GStreamer voice host that no harness here starts. Its build paths
  # reference gcc and glibc's headers and its plugins reference a media stack, all of which would
  # ship in the image. The binaries also carry full symbol tables.
  codexVoiceRuntime = with pkgsUnstable.gst_all_1; [
    gstreamer
    gst-plugins-base
    gst-plugins-good
  ];
  codexWithoutVoice =
    pkgs.runCommand "codex-${pkgsUnstable.codex.version}-without-voice"
      {
        nativeBuildInputs = [
          pkgs.binutils
          pkgs.removeReferencesTo
        ];
      }
      ''
        cp -a ${pkgsUnstable.codex} $out
        chmod -R u+w $out
        rm -r $out/codex-resources/voice
        strip --strip-all $out/bin/*
        # The voice loader's package roots are embedded in bin/codex.
        remove-references-to \
          ${lib.concatMapStringsSep " " (package: "-t ${lib.getLib package}") codexVoiceRuntime} \
          $out/bin/codex
      '';

  # An ExecCondition that passes where the guest config's `console_autologin` (unset is false)
  # equals `autologin`. jq exits 1 otherwise, which skips the unit without failing it; an unreadable
  # config (exit 2 or 5) skips both console units. It prints nothing, which the console, a tty
  # for these units, would show.
  consoleAutologinIs =
    autologin:
    "${pkgs.jq}/bin/jq 'if (.console_autologin == true) == ${lib.boolToString autologin} then empty else (\"\" | halt_error(1)) end' ${runtimeDir}/config.json";
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
  # work on the workspace disk. Every closure byte is pulled to the node before the guest boots.
  virtualisation.diskSize = 8 * 1024;
  # Nobody reads man pages, info or the NixOS manual in a guest whose only interface is the runner.
  documentation.enable = false;
  # disk-image.nix cannot pass copyChannel, whose default puts a second nixpkgs source in the image.
  image.modules.qemu-efi =
    { config, pkgs, ... }:
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

  # Serial console login, selected per environment by the guest config's `console_autologin`: the
  # ordinary login prompt where unset, a root shell without a prompt where set. The guest has no
  # account passwords, so without autologin an attach stops at the prompt.
  systemd.services."serial-getty@ttyS0" = {
    overrideStrategy = "asDropin";
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
    home = "${workspaceDir}/.home";
    shell = pkgs.bash;
  };
  users.mutableUsers = false;
  # The guest accepts no connections: KubeVirt's serial console and the runner's outbound control
  # stream are its only interfaces, so do not create an account password or an SSH key just to
  # satisfy host defaults.
  users.allowNoPasswordLogin = true;

  fileSystems = {
    # The KubeVirt filesystem named `state` is the virtiofs tag.
    "/state" = {
      device = "state";
      fsType = "virtiofs";
      noCheck = true;
      options = [
        "noatime"
        "nodev"
        "nosuid"
      ];
    };
    ${workspaceDir} = {
      device = workspaceDevice;
      fsType = "ext4";
      options = [
        "noatime"
        "nodev"
        "nosuid"
      ];
      autoFormat = true;
      autoResize = false;
    };
  }
  # KubeVirt builds a ConfigMap disk as an ISO 9660 image with Rock Ridge names. `nofail` leaves a
  # missing disk to the runner, which requires its mount, rather than to local-fs.target.
  // lib.listToAttrs (
    map (
      disk:
      lib.nameValuePair "${runtimeDir}/${disk}" {
        device = "/dev/disk/by-id/virtio-agentplane-${disk}";
        fsType = "iso9660";
        noCheck = true;
        options = [
          "ro"
          "nosuid"
          "nodev"
          "noexec"
          "nofail"
        ];
      }
    ) seedDisks
  );

  # Ownership, the setgid mode and inherited harness access are persistent tmpfiles policy rather
  # than imperative chown/chmod/setfacl commands. The service below reapplies it after the mount.
  systemd.tmpfiles.rules = [
    "d ${workspaceDir} 2770 ${agentUser} ${agentUser} -"
    "z ${workspaceDir} 2770 ${agentUser} ${agentUser} -"
    "a+ ${workspaceDir} - - - - d:user:${toString agentId}:rwx,d:group:${toString agentId}:rwx"
    "d ${workspaceDir}/.home 0700 ${agentUser} ${agentUser} -"
    "z ${workspaceDir}/.home 0700 ${agentUser} ${agentUser} -"
    "d ${runtimeDir} 0755 root root -"
  ]
  ++ lib.mapAttrsToList (
    file: disk: "L ${runtimeDir}/${file} - - - - ${runtimeDir}/${disk}/${file}"
  ) seedDiskOf;

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
    # Reapply declarative directory and ACL rules only after the persistent workspace is mounted.
    serviceConfig.ExecStart = "${pkgs.systemd}/bin/systemd-tmpfiles --create --prefix=${workspaceDir}";
  };

  systemd.services.agentplane-runner = {
    description = "Agentplane native harness runner";
    wantedBy = [ "multi-user.target" ];
    requires = [ "agentplane-workspace.service" ];
    after = [
      "agentplane-workspace.service"
      "network.target"
    ];
    unitConfig.RequiresMountsFor = [
      "/state"
      workspaceDir
    ]
    ++ map (disk: "${runtimeDir}/${disk}") seedDisks;
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
    agentplaneCli
    pkgsUnstable.claude-code
    codexWithoutVoice
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
