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
  # Parked GNOME Remote Desktop config; the system listener never worked.
  # Restore with the service, SOPS secrets, and tmpfiles rules below.
  # grdConf = pkgs.writeText "grd-system-rdp.conf" ''
  #   [RDP]
  #   rdp-enabled=true
  #   rdp-server-cert-path=/var/lib/gnome-remote-desktop/rdp-tls.crt
  #   rdp-server-key-path=/var/lib/gnome-remote-desktop/rdp-tls.key
  #   rdp-port=3389
  # '';
  # Parked XRDP session; uncomment this and the service below together.
  # HOME is unset by xrdp-sesman's PAM environment on NixOS.
  # rdpSession = pkgs.writeShellScript "xrdp-xfce-session" ''
  #   export HOME="''${HOME:-/home/agentydragon}"
  #   exec ${pkgs.xfce4-session}/bin/startxfce4 > /tmp/xrdp-wm.log 2>&1
  # '';
in
{
  imports = [
    ../../modules/operator.nix
    ../../modules/vm-desktop.nix
    ../../modules/claude-desktop.nix
    ../../modules/home-wifi.nix
    ../../modules/k8s-workstation.nix
    ../../modules/gpu-monitor.nix
    ../../modules/attic-substituter.nix
    ../../modules/piraeus-host.nix
  ];

  # Claude Desktop Cowork sandboxed-microVM feature (QEMU firmware + virtiofsd
  # at the Debian /usr paths the app probes).
  ducktape.cowork.enable = true;

  ducktape.homeWifi.enable = true;

  # No cellular modem is attached to this VM; keep the NetworkManager default off.
  networking.modemmanager.enable = false;

  ducktape.piraeusHost.enable = true;

  zramSwap.enable = true;
  systemd.oomd.enableUserSlices = true;
  # Swap-based kills require both RAM and swap use to exceed this limit.
  systemd.oomd.settings.OOM.SwapUsedLimit = "75%";
  systemd.slices.user.sliceConfig.ManagedOOMSwap = "kill";
  # Apply oomd.conf changes on a switch instead of waiting for a restart.
  systemd.services.systemd-oomd.reloadTriggers = [
    config.environment.etc."systemd/oomd.conf".source
  ];

  # Idle Bazel servers on this shared box stack up: Bazel's 3h default kept 10 of them,
  # ~22 GiB RSS, on 2026-10-24. Measured 2026-10-24 on devel 7976adcd, cold server and
  # fresh output base, warm shared repo caches: `bazel build --nobuild //...` took
  # 187 s / 109 s / 85 s wall and peaked at 9.0-12.2 GiB server RSS. 15 min idle keeps
  # an active session's server while costing at most ~3 min to rebuild a dropped one.
  # Startup lines only reach direct bazel/bazelisk; local `bb` drops them
  # (devinfra/buildbuddy/docs/bazelrc_startup.md). A user's ~/.bazelrc overrides this.
  ducktape.bazel.extraSystemBazelrc = ''
    startup --max_idle_secs=900
  '';

  # Pull substituter for cache.allegedly.works/{main,gaffer}. Reader JWT is
  # auto-rotated by attic-rotate-wyrm2-reader CronJob; the SOPS file is
  # decryptable by the wyrm2 host key + agentydragon user key.
  ducktape.attic-substituter = {
    enable = true;
    sopsFile = ../../../../secrets/hosts/wyrm2-attic.yaml;
  };

  ducktape.k8sWorker = {
    enableNvidiaRuntime = true;
    nodeLabels = {
      "topology.kubernetes.io/region" = "proxmox";
      "topology.kubernetes.io/zone" = "atlas";
      "csi.proxmox.sinextra.dev/max-volume-attachments" = "29";
      # Kubelet applies this only at registration; the live node was labelled by hand.
      "storage.allegedly.works/piraeus-ready" = "true";
    };
    # nodeTaints = [ "node-role.kubernetes.io/roaming=true:NoSchedule" ];
  };

  # NVIDIA GPU (2x RTX 5090 via VFIO passthrough)
  # Open nvidia module allowlists GPUs by subsystem-ID; Gigabyte RTX 5090 (1458:416f) isn't listed.
  # nvidia-drm modeset HISTORY: modeset=0 was a workaround for the Blackwell
  # VFIO FLR bug (host soft lockups on VM shutdown, see
  # debug/atlas/black_screen_lockup.md). It was accidentally overridden to =1
  # for the entire 28-day host-stable streak (modesetting.enable appended
  # modeset=1 after it, last-wins), so =1 + the other mitigations is the
  # empirically host-stable config. modeset=1 is REQUIRED for the
  # direct-display gaming plan (5090 → monitor DP needs NVIDIA KMS) — see
  # debug/atlas/gpu_strategy.md "Plan: direct display output". A brief
  # deliberate modeset=0 experiment ran 2026-07-02 (guest-lockup hypothesis),
  # abandoned in favor of the display.
  boot.kernelParams = [
    "nvidia.NVreg_OpenRmEnableUnsupportedGpus=1"
  ];
  services.xserver.videoDrivers = [ "nvidia" ];
  hardware.nvidia = {
    modesetting.enable = true; # adds nvidia-drm.modeset=1 (+ fbdev)
    open = true; # Required for Blackwell (RTX 5090) — proprietary module refuses these GPUs
    nvidiaSettings = false; # No X settings app for headless GPU compute
  };
  hardware.nvidia-container-toolkit = {
    enable = true;
    # NixOS defaults the CDI generator to index-named devices (0/1/all), but the
    # k8s device plugin (deviceIDStrategy=uuid, the upstream default) references
    # GPUs by UUID (nvidia.com/gpu=GPU-<uuid>). Harmless on containerd 1.x (CDI
    # off; GPU injection went through the nvidia-container-runtime.cdi env-var
    # handler), but containerd 2.x enables CDI and now resolves those UUID refs
    # against this spec — unresolvable with index names, so GPU pods fail to
    # start with "unresolvable CDI devices". Emit UUID names to match the plugin
    # and keep stable device identity.
    # See cluster/docs/lessons_learned/2026_07_17_gpu_cdi_uuid_naming_containerd2.md
    device-name-strategy = "uuid";
  };

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

  # Ollama with CUDA for local GPU inference — DISABLED. Redundant with the
  # in-cluster ollama pod, which schedules onto wyrm2's own 5090 anyway, so this
  # was a second instance contending for the same GPU rather than a separate-host
  # fallback. Dropping it also avoids the from-source CUDA build (compiles every
  # LLM arch) on nixpkgs bumps. Re-enable if a cluster-down local-inference path
  # is actually needed; models were on the Proxmox CSI PVC (200Gi) / ~/downloads/ollama-models/.
  # pkgs.ollama-cuda
  environment.systemPackages = [
    pkgs.freecad
    pkgs.zoom-us
  ];

  # Podman
  virtualisation.podman.enable = true;

  # Display manager: GDM (inherited from gui.nix; no host override needed).
  #
  # SINGLE-SEAT0 + REMOTE model (2026-07-17, supersedes the old multi-seat design).
  # The physical monitor and a remote console are never needed at the same time, so
  # we do NOT run a second logind seat. The display 5090 (01:00.0) is seat0's sole
  # graphical output — a normal, fully-supported GDM seat0 login, so the physical
  # seat "just works" — and remote access is a *session, not a seat*
  # (remote desktop is currently parked; see nix/debug/wyrm2/remote_desktop/README.md). This
  # retires the whole multi-seat-DM problem: GDM cannot complete a *non*-seat0 user
  # login (gdm!291 unmerged, blocked on systemd#42247), which is exactly the wall
  # the old `seatphysical` seat hit. Full analysis + the retracted multi-seat
  # decision: nix/debug/wyrm2/direct_display_bringup/README.md (the full multiseat saga is in git history).

  # Mutter honors the GPU isolation rules below, keeping the other cards off seat0.
  services.displayManager.defaultSession = "gnome";

  # GNOME Remote Desktop never reached a working system listener on Wyrm2.
  # Restore the parked GRD config, SOPS secrets, and tmpfiles rules together.
  services.gnome.gnome-remote-desktop.enable = false;

  # Parked: uncomment with rdpSession above to restore the pre-login Xfce session.
  # Review the Nebula/firewall access model in nix/debug/wyrm2/remote_desktop/README.md.
  # services.xrdp = {
  #   enable = true;
  #   defaultWindowManager = "${rdpSession}";
  # };
  # services.xserver.desktopManager.xfce.enable = true;

  # Parked GNOME Remote Desktop TLS secrets; encrypted source files are retained.
  # sops.secrets.rdp_tls_cert = {
  #   sopsFile = ../../../../secrets/hosts/wyrm2-rdp-tls.sops.crt;
  #   format = "binary";
  #   path = "/var/lib/gnome-remote-desktop/rdp-tls.crt";
  #   owner = "gnome-remote-desktop";
  #   group = "gnome-remote-desktop";
  #   mode = "0644";
  # };
  # sops.secrets.rdp_tls_key = {
  #   sopsFile = ../../../../secrets/hosts/wyrm2-rdp-tls.sops.key;
  #   format = "binary";
  #   path = "/var/lib/gnome-remote-desktop/rdp-tls.key";
  #   owner = "gnome-remote-desktop";
  #   group = "gnome-remote-desktop";
  #   mode = "0640";
  # };

  # TODO(kvm-no-blank): the pre-SDDM GDM config set login-screen idle-delay=0 to
  # keep the DP output awake so the FV43U KVM would not revert to USB-C before the
  # seatphysical keyboard could wake it. Re-establish that no-blank guarantee for
  # the GDM greeter if the KVM reverts again — see
  # nix/debug/wyrm2/direct_display_bringup/README.md.

  # Steam runs on the display 5090.
  programs.steam.enable = true;
  # Attempt at the native Steam Linux Runtime `libselinux.so.1` failure (Stellaris
  # dies: "mkdir: libselinux.so.1: cannot open shared object file"). Adds it to
  # the Steam FHS env. Only helps if the failing binary runs in the outer FHS,
  # not deep inside the pressure-vessel container — if a game still fails in the
  # SLR sandbox, force Proton on it instead.
  programs.steam.extraPackages = [ pkgs.libselinux ];
  # Parked Sunshine game streaming; see nix/debug/wyrm2/remote_desktop/README.md.
  # services.sunshine = {
  #   enable = true;
  #   package = pkgs.sunshine.override { cudaSupport = true; };
  #   capSysAdmin = true;
  #   openFirewall = true;
  # };
  # Seat0 single-display: hide the virtio-gpu (00:01.0, SPICE console)
  # and the spare 5090 (02:00.0, headless compute) from mutter so seat0 renders
  # only on the display 5090 (01:00.0). This replaces the old seatphysical/seatspare
  # multi-seat rules: multi-GPU mutter with NVIDIA SIGSEGV'd as primary / rendered
  # black as copy target (2026-07-02), so mutter must see exactly one card. Render
  # nodes are not seat- or ignore-gated, so game render-offload still
  # reaches the spare GPU. The TEX Shura keyboard (04d9:0532) needs no rule now — it
  # just defaults to seat0.
  # CAVEAT: `mutter-device-ignore` is honoured by MUTTER ONLY — wlroots/sway ignores
  # it and would grab all three cards (that's why seat0 defaults to GNOME above).
  # virtio stays a plain text/recovery VT console. Gotcha: udev TAGS persist in the
  # udev db — a VM reboot is needed to fully apply a change here.
  # MT7921U Wi-Fi stick (CPAP ez Share sync) — disable USB runtime PM.
  # On 2026-07-25 the stick failed a USB-autosuspend resume (`mt7921u ... resume
  # error -110`), went electrically dark, and stayed dark through four warm
  # reboots (VBUS holds through reboot); only a physical replug at the
  # hypervisor revived it, and cpap-sync was down the whole week. Keeping the
  # device out of autosuspend removes the resume path entirely.
  services.udev.extraRules = ''
    SUBSYSTEM=="drm", KERNEL=="card[0-9]*", KERNELS=="0000:00:01.0", TAG+="mutter-device-ignore"
    SUBSYSTEM=="drm", KERNEL=="card[0-9]*", KERNELS=="0000:02:00.0", TAG+="mutter-device-ignore"
    ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="0e8d", ATTR{idProduct}=="7961", TEST=="power/control", ATTR{power/control}="on"
  '';
  # SPICE audio: increase PipeWire quantum to 2048 to eliminate xruns on the
  # virtual ich9-intel-hda device. Adds ~42ms audio latency (vs ~21ms default),
  # acceptable for media playback. See <nix/debug/wyrm2/spice/audio/README.md>.
  services.pipewire.extraConfig.pipewire."10-spice-quantum" = {
    "context.properties" = {
      "default.clock.quantum" = 2048;
      "default.clock.min-quantum" = 2048;
    };
  };

  # Separate data disks (Proxmox virtio disks).
  # Existing data disks use autoFormat on first boot and autoResize to full size;
  # the cache volume is formatted explicitly before its first activation.
  # Name disks by PCI path, never /dev/vdX: the kernel assigns vdX letters in
  # probe order, so hotplugged disks (and the unused virtio5 slot) shift them
  # across reboots. Proxmox fixes each slot's PCI address (virtio0-4 are
  # 06:0a-06:0e, virtio6-11 are 08:01-08:06).
  # virtio1: 500GB SSD (local-zfs) — Steam library for the gaming seat.
  # Repurposed from the decommissioned Longhorn disk. Games + Proton prefixes must be
  # on SSD: the small-file prefix I/O crawls on the tank-hdd virtiofs share
  # (/mnt/tankshare). See nix/debug/wyrm2/direct_display_bringup/README.md.
  fileSystems."/games" = {
    device = "/dev/disk/by-path/virtio-pci-0000:06:0b.0"; # virtio1
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
  };
  # virtio2 is OpenEBS LVM — managed as LVM VG below, not a filesystem mount
  fileSystems."/var/lib/containerd" = {
    device = "/dev/disk/by-path/virtio-pci-0000:06:0d.0"; # virtio3
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
  };
  fileSystems."/home/agentydragon/.cache/bazel" = {
    device = "/dev/disk/by-path/virtio-pci-0000:06:0e.0"; # virtio4: 300G SSD (local-zfs) — all Bazel caches
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
  };
  fileSystems."/tmp" = {
    device = "/dev/disk/by-path/virtio-pci-0000:08:02.0"; # virtio7: 1T HDD (tank-hdd) — scratch space
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
  # virtio9: 16G HDD (tank-hdd) — WAL of the alloy-node DaemonSet's hostPath
  # (cluster/cdk8s/monitoring/alloy.py). Its own disk so a full /tmp can't stop the WAL
  # buffering through a home outage (30-day window, ~0.15 GB/day measured).
  fileSystems."/var/lib/alloy-node" = {
    device = "/dev/disk/by-path/virtio-pci-0000:08:04.0"; # virtio9
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
  # virtio10: dedicated HDD volume for rebuildable compiler/Python caches.
  # The separate filesystem keeps their growth off root; cache paths stay stable via
  # bind mounts, so existing tools and the running desktop session need no reconfig.
  fileSystems."/var/cache/agentydragon" = {
    device = "/dev/disk/by-label/wyrm2-cache";
    fsType = "ext4";
    autoResize = true;
    options = [
      "nodev"
      "nosuid"
      "nofail"
      "x-systemd.device-timeout=10s"
    ];
  };
  fileSystems."/home/agentydragon/.cache/sccache" = {
    device = "/var/cache/agentydragon/sccache";
    fsType = "none";
    options = [
      "bind"
      "nofail"
      "x-systemd.requires-mounts-for=/var/cache/agentydragon"
    ];
  };
  fileSystems."/home/agentydragon/.cache/uv" = {
    device = "/var/cache/agentydragon/uv";
    fsType = "none";
    options = [
      "bind"
      "nofail"
      "x-systemd.requires-mounts-for=/var/cache/agentydragon"
    ];
  };
  # virtio11: dedicated HDD filesystem for kubelet state and
  # disk-backed emptyDirs. Use the device's path because a blank disk has no
  # filesystem label yet; systemd's makefs option needs to reach the raw device.
  # This replaces the existing /var/lib/kubelet tree, so the initial move must
  # happen during node maintenance with pod mountpoints quiesced; do not activate
  # this over a live pod tree. Any later remount needs the CSI node plugins
  # restarted afterwards: a running one keeps the old directory, and its mounts
  # never reach kubelet (cluster/docs/lessons_learned/2026_10_20_csi_plugin_stale_kubelet_mount.md).
  fileSystems."/var/lib/kubelet" = {
    device = "/dev/disk/by-path/virtio-pci-0000:08:06.0"; # virtio11
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
    # Let the workstation boot if the disk is absent, but kubelet's
    # RequiresMountsFor below keeps it from silently writing state to root.
    options = [
      "nodev"
      "nosuid"
      "nofail"
      "x-systemd.device-timeout=10s"
    ];
  };
  # Keep Kubernetes' standard log path; the bind mount puts logs on the same
  # filesystem as /var/lib/kubelet so kubelet sees one local-ephemeral-storage FS.
  fileSystems."/var/log/pods" = {
    device = "/var/lib/kubelet/pod-logs";
    fsType = "none";
    options = [
      "bind"
      "nofail"
      "x-systemd.requires-mounts-for=/var/lib/kubelet"
    ];
  };

  # Wyrm2's dedicated kubelet disk mounts are nofail so the workstation can
  # boot if the HDD is absent. Kubelet itself must wait for both paths and
  # fail closed rather than writing pod state back onto the root filesystem.
  systemd.services.kubelet.unitConfig.RequiresMountsFor = "/var/lib/kubelet /var/log/pods";

  # Replaces systemd's stock tmp.conf (`q /tmp ... 10d`, `q /var/tmp ... 30d`) so /tmp has a
  # single rule. Keep the /var/tmp line: nothing else cleans it.
  environment.etc."tmpfiles.d/tmp.conf".text = ''
    # Keep the dedicated scratch filesystem bounded without disrupting active files.
    q /tmp 1777 root root 14d
    q /var/tmp 1777 root root 30d
  '';

  # Directories for Bazel caches under the single SSD cache mount.
  systemd.tmpfiles.rules = [
    "d /home/agentydragon/.cache/bazel 0755 agentydragon users -"
    "d /home/agentydragon/.cache/bazel/_bazel_agentydragon 0755 agentydragon users -"
    "d /home/agentydragon/.cache/bazel/_bazel_agentydragon/cache 0755 agentydragon users -"
    "d /home/agentydragon/.cache/bazel/_bazel_agentydragon/cache/repos 0755 agentydragon users -"
    # Steam library mount (virtio1) must be user-writable; the fresh ext4 root
    # is created root:root, so chown it after the mount lands.
    "d /games 0755 agentydragon users -"
    # Dedicated cache volume mount and bind-mount targets.
    "d /var/cache/agentydragon 0755 agentydragon users -"
    "d /var/cache/agentydragon/sccache 0755 agentydragon users -"
    "d /var/cache/agentydragon/uv 0755 agentydragon users -"
    "d /home/agentydragon/.cache/sccache 0755 agentydragon users -"
    "d /home/agentydragon/.cache/uv 0755 agentydragon users -"
    # Parked GRD runtime config; restore alongside grdConf and the service.
    # "d /var/lib/gnome-remote-desktop/.local/share/gnome-remote-desktop 0755 gnome-remote-desktop gnome-remote-desktop -"
    # "L+ /var/lib/gnome-remote-desktop/.local/share/gnome-remote-desktop/grd.conf - - - - ${grdConf}"
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
    openssh.authorizedKeys.keys = sshKeys ++ [ keys.wyrm2McpAgentydragon ];
    extraGroups = [ "systemd-journal" ];
  };

  users.users.root.openssh.authorizedKeys.keys = sshKeys ++ [ keys.wyrm2McpRoot ];
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
