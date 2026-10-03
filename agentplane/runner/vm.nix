# NixOS guest for the KubeVirt-backed Agentplane runner.
#
# The containerDisk supplies only this replaceable root filesystem. Native harness state and the
# runner journal live on the retained /state disk; checkout contents and tool caches live on
# /workspace. State uses XFS project quota; the separate workspace volume is ext4. Both are CDI
# blank DataVolumes initialized only with explicit, one-shot config disk authorization.
{
  lib,
  pkgs,
  pkgsUnstable,
  agentplaneRunner,
  ...
}:
let
  configDevice = "/dev/disk/by-id/virtio-agentplane-config";
  stateDevice = "/dev/disk/by-id/virtio-state";
  workspaceDevice = "/dev/disk/by-id/virtio-workspace";
  runtimeDir = "/run/agentplane";
  sourceDir = "${runtimeDir}/config-source";
  publicCa = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
  agentGroup = "agentplane";
  serviceGroup = "agentplane-runner";
  agentUser = "runner";
  runnerUser = "agentplane-runner";

  storagePreparation = pkgs.writeShellScript "agentplane-prepare-disks" ''
    set -euo pipefail
    authorized="$(${agentplaneRunner}/bin/agentplane-runner-validate-config)"

    wait_for_device() {
      local device="$1"
      for _ in $(seq 1 60); do
        if [ -b "$device" ]; then
          return 0
        fi
        sleep 1
      done
      echo "KubeVirt persistent disk did not appear at $device" >&2
      return 1
    }

    prepare_disk() {
      local name="$1" device="$2" expected_type="$3" expected_label="$4" type label status signatures
      wait_for_device "$device"
      if type="$(blkid -p -s TYPE -o value "$device" 2>/dev/null)"; then
        label="$(blkid -p -s LABEL -o value "$device" 2>/dev/null || true)"
        signatures="$(wipefs --no-act --noheadings --output TYPE "$device")"
        if [ "$type" != "$expected_type" ] || [ "$label" != "$expected_label" ] || [ "$signatures" != "$expected_type" ]; then
          echo "Refusing unexpected filesystem on $name disk ($type, label $label)" >&2
          return 1
        fi
        return 0
      else
        status=$?
        if [ "$status" -ne 2 ]; then
          echo "Could not inspect $name disk (blkid status $status)" >&2
          return 1
        fi
      fi

      signatures="$(wipefs --no-act --noheadings "$device")"
      if [ -n "$signatures" ]; then
        echo "Refusing unrecognized disk signatures on $name disk" >&2
        return 1
      fi
      if ! printf '%s\n' "$authorized" | grep -Fxq "$name"; then
        echo "Refusing to format blank-looking $name disk without one-shot authorization" >&2
        return 1
      fi
      # No known signatures is not proof that a newly provisioned disk is empty. CDI blank
      # DataVolumes are zero-filled, so scan the complete device before the one-shot authorization
      # permits formatting it. Formatted retained volumes take the signature/label path above.
      if cmp --silent -n "$(blockdev --getsize64 "$device")" "$device" /dev/zero; then
        :
      else
        status=$?
        if [ "$status" -eq 1 ]; then
          echo "Refusing to format non-zero $name disk without a recognized filesystem" >&2
        else
          echo "Could not verify blank $name disk (cmp status $status)" >&2
        fi
        return 1
      fi
      if [ "$expected_type" = xfs ]; then
        mkfs.xfs -f -L "$expected_label" "$device"
      else
        mkfs.ext4 -F -L "$expected_label" "$device"
      fi
    }

    prepare_disk state "${stateDevice}" xfs APSTATE
    prepare_disk workspace "${workspaceDevice}" ext4 APWORKSPACE
  '';
in
{
  imports = [ ./../../nix/nixos/modules/vm-hardware.nix ];

  networking.hostName = "agentplane-runner-vm";
  networking.firewall.allowedTCPPorts = [ 7000 ];
  services.openssh.enable = lib.mkForce false;
  services.qemuGuest.enable = true;

  # Keep the replaceable guest root small. KubeVirt stores runner journals and agent work on the
  # two separately sized persistent disks below.
  virtualisation.diskSize = 8 * 1024;
  boot.kernelModules = [ "xfs" ];
  boot.kernelParams = [ "console=ttyS0,115200n8" ];

  users.groups.${agentGroup} = {
    gid = 1000;
  };
  users.groups.${serviceGroup} = {
    gid = 1001;
  };
  users.users.${agentUser} = {
    isSystemUser = true;
    uid = 1000;
    group = agentGroup;
    home = "/workspace/home";
    shell = pkgs.bash;
  };
  users.users.${runnerUser} = {
    isSystemUser = true;
    uid = 1001;
    group = serviceGroup;
    extraGroups = [ agentGroup ];
    home = "/workspace/home";
    shell = pkgs.bash;
  };
  users.mutableUsers = false;
  # This guest has no interactive access path. KubeVirt and the runner gRPC listener are its only
  # interfaces, so do not create an account password or an SSH key just to satisfy host defaults.
  users.allowNoPasswordLogin = true;

  fileSystems."/state" = {
    device = stateDevice;
    fsType = "xfs";
    options = [
      "noatime"
      "nodev"
      "nosuid"
      "prjquota"
    ];
    autoFormat = false;
    autoResize = false;
  };
  fileSystems."/workspace" = {
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
    description = "Read the Agentplane public guest configuration disk";
    wantedBy = [ "local-fs.target" ];
    before = [
      "agentplane-disk-prepare.service"
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
      mounted=0
      for _ in $(seq 1 60); do
        if [ -b "${configDevice}" ] && mount -o ro,nosuid,nodev,noexec "${configDevice}" "${sourceDir}" 2>/dev/null; then
          mounted=1
          break
        fi
        sleep 1
      done
      if [ "$mounted" -ne 1 ]; then
        echo "KubeVirt read-only Agentplane config disk missing at ${configDevice}" >&2
        exit 1
      fi
      for file in config.json ca-certificates.crt kubeconfig; do
        test -s "${sourceDir}/$file" || { echo "Agentplane config disk is missing $file" >&2; exit 1; }
      install -m0644 "${sourceDir}/$file" "${runtimeDir}/$file"
      done
      install -m0644 "${sourceDir}/ca-certificates.crt" "${runtimeDir}/proxy-ca.crt"
      cat "${publicCa}" > "${runtimeDir}/ca-bundle.crt.tmp"
      printf '\n' >> "${runtimeDir}/ca-bundle.crt.tmp"
      cat "${runtimeDir}/proxy-ca.crt" >> "${runtimeDir}/ca-bundle.crt.tmp"
      chmod 0644 "${runtimeDir}/ca-bundle.crt.tmp"
      mv "${runtimeDir}/ca-bundle.crt.tmp" "${runtimeDir}/ca-certificates.crt"
      umount "${sourceDir}"
      rmdir "${sourceDir}"
      trap - EXIT
    '';
  };

  systemd.services.agentplane-disk-prepare = {
    description = "Format only authorized blank Agentplane persistent disks";
    requires = [ "agentplane-config.service" ];
    after = [ "agentplane-config.service" ];
    before = [
      "state.mount"
      "workspace.mount"
      "local-fs.target"
    ];
    requiredBy = [
      "state.mount"
      "workspace.mount"
    ];
    unitConfig.DefaultDependencies = false;
    path = [
      pkgs.coreutils
      pkgs.diffutils
      pkgs.e2fsprogs
      pkgs.gnugrep
      pkgs.xfsprogs
      pkgs.util-linux
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = "${storagePreparation}";
  };

  systemd.services.agentplane-storage = {
    description = "Set Agentplane persistent storage boundaries";
    requires = [
      "state.mount"
      "workspace.mount"
    ];
    after = [
      "state.mount"
      "workspace.mount"
    ];
    before = [ "agentplane-runner.service" ];
    path = [
      pkgs.acl
      pkgs.bash
      pkgs.coreutils
      pkgs.shadow
      pkgs.util-linux
      pkgs.xfsprogs
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -euo pipefail
      chown ${runnerUser}:${serviceGroup} /state
      chmod 0710 /state
      setfacl -m g:${agentGroup}:--x /state
      chown ${runnerUser}:${agentGroup} /workspace
      chmod 2770 /workspace
      install -d -m2770 -o ${agentUser} -g ${agentGroup} /workspace/home
      install -d -m2770 -o ${runnerUser} -g ${agentGroup} /state/native
      setfacl -m g:${agentGroup}:rwx,d:g:${agentGroup}:rwx,d:m:rwx /state/native

      # Check the actual service/agent identities and the permissions new history files inherit.
      probe=/state/native/.permission-probe
      install -d -m2770 -o ${runnerUser} -g ${agentGroup} "$probe"
      setfacl -m g:${agentGroup}:rwx,d:g:${agentGroup}:rwx,d:m:rwx "$probe"
      runuser -u ${runnerUser} -- sh -eu -c 'umask 0007; touch "$1"' _ "$probe/service-created"
      runuser -u ${agentUser} -- sh -eu -c 'umask 0007; touch "$1"' _ "$probe/agent-created"
      runuser -u ${agentUser} -- test -w "$probe/service-created"
      runuser -u ${runnerUser} -- test -w "$probe/agent-created"
      rm -rf "$probe"

      # Native transcript/config writes share this tree. The separate project ID and hard limits
      # leave the rest of /state available for the runner's SQLite journals and metadata.
      xfs_quota -x -c 'project -s -p /state/native 801' /state
      xfs_quota -x -c 'limit -p bhard=8g ihard=200000 801' /state
    '';
  };

  systemd.services.agentplane-trust = {
    description = "Build the Agentplane Java trust store from the public guest CA bundle";
    wantedBy = [ "multi-user.target" ];
    requires = [ "agentplane-config.service" ];
    after = [ "agentplane-config.service" ];
    before = [ "agentplane-runner.service" ];
    path = [
      pkgs.coreutils
      pkgs.findutils
      pkgs.gawk
      pkgs.jdk
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -euo pipefail
      store="${runtimeDir}/java-cacerts"
      cert_dir="${runtimeDir}/java-certs"
      rm -f "$store"
      mkdir -p "$cert_dir"
      awk -v out="$cert_dir" '
        /BEGIN CERTIFICATE/ { n++; file = sprintf("%s/cert-%03d.pem", out, n) }
        file != "" { print > file }
        /END CERTIFICATE/ { close(file); file = "" }
      ' "${runtimeDir}/ca-certificates.crt"
      test -n "$(find "$cert_dir" -type f -name '*.pem' -print -quit)" || {
        echo "Agentplane proxy CA bundle contains no PEM certificates" >&2
        exit 1
      }
      for cert in "$cert_dir"/*.pem; do
        keytool -importcert -noprompt -storepass changeit \
          -alias "agentplane-ca-$(basename "$cert" .pem)" -keystore "$store" -file "$cert"
      done
      rm -rf "$cert_dir"
      chmod 0644 "$store"
    '';
  };

  systemd.services.agentplane-runner = {
    description = "Agentplane native harness runner";
    wantedBy = [ "multi-user.target" ];
    requires = [
      "agentplane-config.service"
      "agentplane-trust.service"
      "agentplane-storage.service"
      "state.mount"
      "workspace.mount"
    ];
    after = [
      "agentplane-config.service"
      "agentplane-trust.service"
      "agentplane-storage.service"
      "state.mount"
      "workspace.mount"
      "network.target"
    ];
    path = [
      agentplaneRunner
      pkgs.coreutils
      pkgs.systemd
    ];
    environment = {
      HOME = "/workspace/home";
      ANTHROPIC_AUTH_TOKEN = "agentplane-credential-agentplane-workload";
      OPENAI_API_KEY = "agentplane-credential-agentplane-workload";
    };
    serviceConfig = {
      Type = "simple";
      ExecStart = "${agentplaneRunner}/bin/agentplane-runner-vm";
      User = runnerUser;
      Group = serviceGroup;
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
      CapabilityBoundingSet = [
        "CAP_KILL"
        "CAP_SETGID"
        "CAP_SETUID"
      ];
      AmbientCapabilities = [
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
        "/workspace"
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
    xfsprogs
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
