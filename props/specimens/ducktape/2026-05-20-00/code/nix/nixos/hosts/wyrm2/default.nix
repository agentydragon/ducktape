# Wyrm2 - NixOS dev workstation VM + k8s worker
# Similar to rugged (Dell Rugged tablet) but for Proxmox VM.
# Joins the Talos k8s cluster via Nebula mesh.
{
  config,
  pkgs,
  lib,
  username,
  ...
}:
let
  keys = import ../../../ssh-keys.nix;
  sshKeys = with keys; [
    wyrm2
    atlas
    rugged
    rugged_wyrm
  ];
in
{
  imports = [
    ../../modules/gui.nix
    ../../modules/workstation.nix
    ../../modules/bazel
    ../../modules/system-inspection-sudo.nix
    ../../modules/k8s-worker.nix
    ../../modules/gpu-monitor.nix
    ../../modules/attic-substituter.nix
  ];

  # Passwordless sudo for system inspection commands
  ducktape.systemInspectionSudo.enable = true;

  # Pull substituter for cache.allegedly.works/{main,gaffer}. Reader JWT is
  # auto-rotated by attic-rotate-wyrm2-reader CronJob; the SOPS file is
  # decryptable by the wyrm2 host key + agentydragon user key.
  ducktape.attic-substituter = {
    enable = true;
    sopsFile = ../../../../secrets/hosts/wyrm2-attic.yaml;
  };

  ducktape.k8sWorker = {
    enable = true;
    enableNvidiaRuntime = true;
    nodeLabels = {
      "topology.kubernetes.io/region" = "proxmox";
      "topology.kubernetes.io/zone" = "atlas";
      "csi.proxmox.sinextra.dev/max-volume-attachments" = "29";
      "node.longhorn.io/create-default-disk" = "true";
    };
    # nodeTaints = [ "node-role.kubernetes.io/roaming=true:NoSchedule" ];
  };

  # NVIDIA GPU (2x RTX 5090 via VFIO passthrough)
  # Open nvidia module allowlists GPUs by subsystem-ID; Gigabyte RTX 5090 (1458:416f) isn't listed.
  # nvidia-drm modeset=0: Workaround for Blackwell (RTX 5090) VFIO FLR bug — the GPU
  # fails Function Level Reset after VM shutdown, causing host soft lockups. Disabling
  # nvidia-drm modesetting prevents the driver from taking a KMS master reference that
  # complicates FLR teardown. See debug/atlas/black_screen_lockup.md.
  boot.kernelParams = [
    "nvidia.NVreg_OpenRmEnableUnsupportedGpus=1"
    "nvidia-drm.modeset=0"
  ];
  services.xserver.videoDrivers = [ "nvidia" ];
  hardware.nvidia = {
    modesetting.enable = true;
    open = true; # Required for Blackwell (RTX 5090) — proprietary module refuses these GPUs
    nvidiaSettings = false; # No X settings app for headless GPU compute
  };
  hardware.nvidia-container-toolkit.enable = true;

  # GPU health monitoring — periodic telemetry + dmesg error watcher.
  # See debug/atlas/gpu_lockup_20260417/README.md for context.
  ducktape.gpuMonitor.enable = true;

  # MT7921 USB WiFi stick firmware (for CPAP ez Share sync)
  hardware.firmware = [ pkgs.linux-firmware ];

  # RTL-SDR Blog V4 — blacklist dvb_usb_rtl28xxu kernel module and install
  # udev rules for non-root USB access to the RTL2838 dongle.
  hardware.rtl-sdr.enable = true;

  # CPAP ez Share WiFi network — NM connection rendered from SOPS secret.
  # never-default=true keeps wyrm2's default route on ens18.
  # TODO: Consider cleaner secret integration — sops NM secret agent, or
  # materializing SOPS secrets into GNOME keyring so networking.networkmanager.ensureProfiles
  # can be used without inlining the password into the rendered keyfile.
  sops.secrets.cpap_wifi_password = {
    sopsFile = ../../../../secrets/shared/cpap-ezshare.yaml;
    key = "wifi_password";
  };
  sops.templates.cpap_nm_connection = {
    content = ''
      [connection]
      id=cpap-ezshare
      type=wifi

      [wifi]
      ssid=Rai CPAP ez Share
      mode=infrastructure

      [wifi-security]
      key-mgmt=wpa-psk
      psk=${config.sops.placeholder.cpap_wifi_password}

      [ipv4]
      method=auto
      never-default=true
      dns-search=~ezshare.card

      [ipv6]
      method=ignore
    '';
    path = "/etc/NetworkManager/system-connections/cpap-ezshare.nmconnection";
    mode = "0600";
  };

  # Ollama with CUDA for local GPU inference (also used by k8s ollama pod, but
  # useful standalone when cluster is down or for ad-hoc tasks).
  # Models stored on Proxmox CSI PVC (200Gi) or ~/downloads/ollama-models/.
  environment.systemPackages = [
    pkgs.ollama-cuda
    pkgs.lvm2_dmeventd # LVM tools with dmeventd client support for thin pool autoextend
    pkgs.freecad
  ];

  # Podman
  virtualisation.podman.enable = true;

  # GNOME 49 dropped X11 sessions — Wayland is the only option.
  # NixOS auto-disables Wayland for NVIDIA, but the display is QXL (not NVIDIA).
  services.displayManager.gdm.wayland = true;

  # SPICE audio: increase PipeWire quantum to 2048 to eliminate xruns on the
  # virtual ich9-intel-hda device. Adds ~42ms audio latency (vs ~21ms default),
  # acceptable for media playback. See <debug/atlas/spice_audio/README.md>.
  services.pipewire.extraConfig.pipewire."10-spice-quantum" = {
    "context.properties" = {
      "default.clock.quantum" = 2048;
      "default.clock.min-quantum" = 2048;
    };
  };

  # Separate data disks (Proxmox virtio disks).
  # autoFormat creates ext4 on first boot; autoResize grows to full disk size.
  # virtio0=/dev/vda, virtio1=/dev/vdb, virtio2=/dev/vdc, virtio3=/dev/vdd,
  # virtio4=/dev/vde, virtio5=/dev/vdf, virtio7=/dev/vdh
  fileSystems."/var/local-path-provisioner" = {
    device = "/dev/vda";
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
  };
  fileSystems."/var/mnt/longhorn" = {
    device = "/dev/vdb";
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
  };
  # virtio2 (/dev/vdc) is OpenEBS LVM — managed as LVM VG below, not a filesystem mount
  fileSystems."/var/lib/containerd" = {
    device = "/dev/vdd";
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
  };
  fileSystems."/home/agentydragon/.cache/bazel" = {
    device = "/dev/vde"; # 150G SSD (local-zfs) — Bazel output bases
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
  };
  fileSystems."/home/agentydragon/.cache/bazel/_bazel_agentydragon/cache/repos" = {
    device = "/dev/vdf"; # 100G HDD (tank-hdd) — Bazel repository cache
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
  };
  fileSystems."/tmp" = {
    device = "/dev/vdh"; # 1T HDD (tank-hdd) — scratch space
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
    options = [
      "nodev"
      "nosuid"
      "nofail"
      "x-systemd.device-timeout=10s"
    ];
  };

  # LVM for OpenEBS LVM LocalPV — thin-provisioned volumes with snapshot support.
  # OpenEBS node agent runs privileged and uses host LVM tools via nsenter.
  boot.kernelModules = [ "dm_thin_pool" ];

  # dmeventd monitors thin pools and auto-extends them before they fill up.
  # Without this, OpenEBS creates a tiny thin pool (sized to the first PVC)
  # and mke2fs fails once it's full.
  services.lvm.dmeventd.enable = true;
  environment.etc."lvm/lvm.conf".text = lib.mkAfter ''
    activation {
      thin_pool_autoextend_threshold = 75
      thin_pool_autoextend_percent = 20
    }
  '';

  # OpenEBS LVM volume groups — idempotent oneshot services that create PV + VG.
  #   openebs-proxmox-ssd: virtio2 (/dev/vdc) — 500GB NVMe (local-zfs)
  #   openebs-proxmox-hdd: virtio6 (/dev/vdg) — 500GB HDD (tank-hdd)
  systemd.services =
    lib.mapAttrs'
      (
        vg: dev:
        lib.nameValuePair "openebs-${vg}-setup" {
          description = "Initialize LVM VG openebs-proxmox-${vg} on ${dev}";
          wantedBy = [ "multi-user.target" ];
          before = [ "kubelet.service" ];
          after = [ "systemd-udev-settle.service" ];
          path = [ pkgs.lvm2_dmeventd ];
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
          };
          script = ''
            if [ ! -b ${dev} ]; then
              echo "Device ${dev} not present, skipping VG setup"
              exit 0
            fi
            if vgs openebs-proxmox-${vg} >/dev/null 2>&1; then
              echo "VG openebs-proxmox-${vg} already exists, activating"
            else
              pvcreate ${dev}
              vgcreate openebs-proxmox-${vg} ${dev}
            fi
            vgchange -ay openebs-proxmox-${vg}

            # Enable dmeventd monitoring on thin pools for autoextend.
            # OpenEBS's container can't reach the host dmeventd socket, so pools
            # are created without monitoring (openebs/lvm-localpv#93, WONTFIX).
            # On boot, lvm2-monitor.service runs before VGs are active so misses them.
            for pool in $(lvs --noheadings -o lv_name -S "lv_attr=~^t,vg_name=openebs-proxmox-${vg}" | tr -d ' '); do
              echo "Enabling dmeventd monitoring on openebs-proxmox-${vg}/$pool"
              lvchange --monitor y "openebs-proxmox-${vg}/$pool"
            done
          '';
        }
      )
      {
        ssd = "/dev/vdc";
        hdd = "/dev/vdg";
      };

  # Intermediate directories for the nested repo cache mount.
  # The SSD disk is mounted at ~/.cache/bazel, then the HDD disk is mounted
  # over the repo cache subdirectory inside it.
  systemd.tmpfiles.rules = [
    "d /home/agentydragon/.cache/bazel 0755 agentydragon users -"
    "d /home/agentydragon/.cache/bazel/_bazel_agentydragon 0755 agentydragon users -"
    "d /home/agentydragon/.cache/bazel/_bazel_agentydragon/cache 0755 agentydragon users -"
    "d /home/agentydragon/.cache/bazel/_bazel_agentydragon/cache/repos 0755 agentydragon users -"
    "d /tmp 1777 root root -"
  ];

  # virtiofs shared from Proxmox host (atlas)
  fileSystems."/mnt/tankshare" = {
    device = "tankshare";
    fsType = "virtiofs";
    options = [
      "defaults"
      "_netdev"
      "nofail"
    ];
  };

  fileSystems."/code" = {
    device = "code";
    fsType = "virtiofs";
    options = [
      "defaults"
      "_netdev"
      "nofail"
    ];
  };

  services = {
    avahi = {
      enable = true;
      nssmdns4 = true;
    };
    printing.enable = true;
  };

  # User configuration
  users.users.${username} = {
    shell = pkgs.zsh;
    openssh.authorizedKeys.keys = sshKeys;
    extraGroups = [ "systemd-journal" ];
  };

  users.users.root.openssh.authorizedKeys.keys = sshKeys;
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  # SPICE USB redirection helper (setuid root for USB device passthrough)
  security.wrappers.spice-client-glib-usb-acl-helper = {
    setuid = true;
    owner = "root";
    group = "root";
    source = "${pkgs.spice-gtk}/bin/spice-client-glib-usb-acl-helper";
  };

  # MOTD
  users.motd = "🐉 Welcome to wyrm2!\n";
}
