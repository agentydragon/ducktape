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
  # gnome-remote-desktop's system daemon reads its config from
  # $XDG_DATA_HOME/gnome-remote-desktop/grd.conf
  # (= /var/lib/gnome-remote-desktop/.local/share/gnome-remote-desktop/grd.conf) under
  # the [RDP] group (keys rdp-enabled / rdp-server-cert-path / rdp-server-key-path).
  # Declaring RDP enabled here is what makes the daemon bind 3389 at startup — and it
  # sidesteps `grdctl --system rdp enable`, whose built-in systemd-enable step can't
  # write to read-only /etc on NixOS. Symlinked into place by tmpfiles (below); the
  # TLS pair is a SOPS secret.
  grdConf = pkgs.writeText "grd-system-rdp.conf" ''
    [RDP]
    rdp-enabled=true
    rdp-server-cert-path=/var/lib/gnome-remote-desktop/rdp-tls.crt
    rdp-server-key-path=/var/lib/gnome-remote-desktop/rdp-tls.key
    rdp-port=3389
  '';
  # xrdp launches the window manager in a session env that, on NixOS, does NOT set
  # HOME (xrdp-sesman's pam line is `pam_env … readenv=0`), so a full desktop like
  # Xfce fails to start and you get a black screen. This wrapper sets HOME, launches
  # Xfce, and tees its output to /tmp/xrdp-wm.log so any remaining failure is visible
  # (nixpkgs's xrdp test sidesteps this with bare WMs that don't need HOME).
  rdpSession = pkgs.writeShellScript "xrdp-xfce-session" ''
    export HOME="''${HOME:-/home/agentydragon}"
    exec ${pkgs.xfce.xfce4-session}/bin/startxfce4 > /tmp/xrdp-wm.log 2>&1
  '';
in
{
  imports = [
    ../../modules/gui.nix
    ../../modules/workstation.nix
    ../../modules/bazel
    ../../modules/system-inspection-sudo.nix
    ../../modules/claude-desktop.nix
    ../../modules/home-wifi.nix
    ../../modules/k8s-worker.nix
    ../../modules/gpu-monitor.nix
    ../../modules/github-api-recorder.nix
    ../../modules/hostexecd.nix
    ../../modules/attic-substituter.nix
  ];

  # Passwordless sudo for system inspection commands
  ducktape.systemInspectionSudo.enable = true;

  # Claude Desktop Cowork sandboxed-microVM feature (QEMU firmware + virtiofsd
  # at the Debian /usr paths the app probes).
  ducktape.cowork.enable = true;

  ducktape.homeWifi.enable = true;

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
      # Scopes the journal-only promtail DaemonSet to systemd/journald nodes
      # (cluster/k8s/monitoring/loki/promtail-journal-helmrelease.yaml).
      "node-vendor" = "nixos";
    };
    # nodeTaints = [ "node-role.kubernetes.io/roaming=true:NoSchedule" ];
  };

  # hostexecd: haku-console runs approved commands here under the operator's own
  # Authentik identity and outbound daemon id derive from networking.hostName.
  # See nix/nixos/modules/hostexecd.nix.
  ducktape.hostexec.enable = true;

  # NVIDIA GPU (2x RTX 5090 via VFIO passthrough)
  # Open nvidia module allowlists GPUs by subsystem-ID; Gigabyte RTX 5090 (1458:416f) isn't listed.
  # nvidia-drm modeset HISTORY: modeset=0 was a workaround for the Blackwell
  # VFIO FLR bug (host soft lockups on VM shutdown, see
  # debug/atlas/black_screen_lockup.md). It was accidentally overridden to =1
  # for the entire 28-day host-stable streak (modesetting.enable appended
  # modeset=1 after it, last-wins), so =1 + the other mitigations is the
  # empirically host-stable config. modeset=1 is REQUIRED for the
  # direct-display gaming plan (5090 → monitor DP needs NVIDIA KMS) — see
  # debug/atlas/gpu-strategy.md "Plan: direct display output". A brief
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

  # Attribution for the GitHub GraphQL quota drain; see the module header.
  ducktape.githubApiRecorder.enable = true;

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
    pkgs.lvm2_dmeventd # LVM tools with dmeventd client support for thin pool autoextend
    pkgs.freecad
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
  # (gnome-remote-desktop, below; reach it over an SSH-key tunnel on nebula). This
  # retires the whole multi-seat-DM problem: GDM cannot complete a *non*-seat0 user
  # login (gdm!291 unmerged, blocked on systemd#42247), which is exactly the wall
  # the old `seatphysical` seat hit. Full analysis + the retracted multi-seat
  # decision: debug/atlas/direct_display_bringup/README.md (full multiseat saga archived under archive/).

  # seat0 defaults to GNOME. mutter is what honours `mutter-device-ignore` (see the
  # udev rules below), so the single-display isolation only works under
  # GNOME/mutter — NOT sway, which (via wlroots) claims every seat DRM card and
  # ignores that tag. programs.sway stays enabled for experimentation, but a sway
  # seat0 login would grab all three GPUs until pinned with WLR_DRM_DEVICES.
  services.displayManager.defaultSession = "gnome";

  # CLEANUP(added 2026-07-17): remove once gnome-remote-desktop headless login is
  # confirmed working on this NVIDIA host. GDM's daemon is silent after PAM without
  # this, so any greeter→session failure reads as an inscrutable freeze — keep
  # verbose logging on through the GRD bring-up (the nixpkgs#504490 handover fix is
  # already in gsd 50.1, but the NVIDIA-headless GRD path is untested here).
  # Recovery for the leaked-session zombie this can expose:
  # debug/atlas/direct_display_bringup/login_zombie_recovery.md.
  services.displayManager.gdm.debug = true;

  # Remote access = a *session, not a seat*: gnome-remote-desktop "Remote Login"
  # (system RDP with GDM handover) starts a fresh headless GNOME session on connect
  # — no prior local login needed, and its virtual monitor resizes to the client.
  # Reach it over an SSH-key tunnel on nebula (`ssh -L 3389:localhost:3389
  # <wyrm2-nebula>`, then RDP to localhost:3389): RDP is NOT firewalled open, so the
  # only path in is the key-only SSH already configured — nebula cert + SSH key are
  # the gates, the RDP password is just the login step. RDP is enabled declaratively:
  # the TLS pair is a SOPS secret (below) and grd.conf (grdConf above) is symlinked
  # into place by tmpfiles — no grdctl, no runtime bootstrap. See the GRD bring-up
  # notes in debug/atlas/direct_display_bringup/README.md.
  services.gnome.gnome-remote-desktop.enable = true;

  # ACTUAL remote desktop: xrdp + Xfce. gnome-remote-desktop system "Remote Login"
  # above is the GNOME-native headless path but is BLOCKED on NixOS (`grdctl enable`
  # self-systemd-enables into read-only /etc → EROFS; GDM handover broken — nixpkgs
  # #504490/#535360; see debug/atlas/remote-desktop-wyrm2.md), so it never listens and
  # stays dormant. xrdp spawns its own X session on connect (PAM auth with your system
  # password — no stored creds), which works pre-login WITHOUT auto-login. Tunnel-only:
  # services.xrdp.openFirewall defaults false, so 3389 is not exposed on nebula — reach
  # it via `ssh -L 3390:localhost:3389 wyrm2` + an RDP client (freerdp3 on rugged).
  # Separate X11 Xfce session, not the GPU/Wayland seat (use Sunshine for that, when
  # logged in).
  services.xrdp = {
    enable = true;
    defaultWindowManager = "${rdpSession}";
  };
  services.xserver.desktopManager.xfce.enable = true;

  # The self-signed TLS pair RDP mandates. Host-only secret (admin + wyrm2-host), like
  # the Nebula host keys — the daemon decrypts at activation via the host key. Rendered
  # straight to the daemon's paths, owned by the gnome-remote-desktop user so it can
  # read the key.
  sops.secrets.rdp_tls_cert = {
    sopsFile = ../../../../secrets/hosts/wyrm2-rdp-tls.sops.crt;
    format = "binary";
    path = "/var/lib/gnome-remote-desktop/rdp-tls.crt";
    owner = "gnome-remote-desktop";
    group = "gnome-remote-desktop";
    mode = "0644";
  };
  sops.secrets.rdp_tls_key = {
    sopsFile = ../../../../secrets/hosts/wyrm2-rdp-tls.sops.key;
    format = "binary";
    path = "/var/lib/gnome-remote-desktop/rdp-tls.key";
    owner = "gnome-remote-desktop";
    group = "gnome-remote-desktop";
    mode = "0640";
  };

  # grd.conf (grdConf above) is symlinked into the daemon's XDG_DATA_HOME by the
  # systemd.tmpfiles.rules block below — fully declarative, no oneshot.

  # TODO(kvm-no-blank): the pre-SDDM GDM config set login-screen idle-delay=0 to
  # keep the DP output awake so the FV43U KVM would not revert to USB-C before the
  # seatphysical keyboard could wake it. Re-establish that no-blank guarantee for
  # the GDM greeter if the KVM reverts again — see
  # debug/atlas/direct_display_bringup/README.md.

  # Sway kept as an OPTIONAL seat0 session (NOT the default — GNOME is, because only
  # mutter honours the mutter-device-ignore isolation). A real WM to game/debug from,
  # run as agentydragon. NVIDIA: --unsupported-gpu is mandatory; hardware cursors off
  # (wlroots can't do them on this NVIDIA path).
  # CAVEAT (single-seat0): wlroots ignores mutter-device-ignore, so a sway seat0
  # login enumerates ALL THREE seat0 cards (virtio + both 5090s) and picks the wrong
  # one / "Found N GPUs". To actually use sway here it must be pinned to the display
  # 5090 (01:00.0) via a colon-free WLR_DRM_DEVICES symlink — NOT yet wired. Open
  # sway-on-seat0 question tracked in the decision doc.
  programs.sway = {
    enable = true;
    wrapperFeatures.gtk = true;
    extraOptions = [ "--unsupported-gpu" ];
    extraPackages = with pkgs; [
      foot
      wofi
      wl-clipboard
      swayidle
      swaylock
      dex # runs XDG ~/.config/autostart entries under sway
      grim # screenshots (wlroots)
      slurp # region select for grim
    ];
  };
  # NVIDIA: wlroots can't do hardware cursors on this path. Kept for the optional
  # sway seat0 session above; harmless for the default GNOME/mutter session.
  environment.sessionVariables.WLR_NO_HARDWARE_CURSORS = "1";

  # Steam — games run on the RTX 5090s (direct display via seatphysical, or
  # streamed to atlas via Sunshine/Moonlight).
  # Games run directly in the sway session on seatphysical (the display GPU is
  # 01:00.0 = the same GPU DXVK renders on, so no gamescope GPU-pinning is
  # needed — see debug/atlas/direct_display_bringup/README.md). No gamescope kiosk
  # session: on this 2-identical-5090 box gamescope can't disambiguate the
  # GPUs and its greeter session crashed opening the wrong (seat0-owned) card.
  programs.steam.enable = true;
  # Attempt at the native Steam Linux Runtime `libselinux.so.1` failure (Stellaris
  # dies: "mkdir: libselinux.so.1: cannot open shared object file"). Adds it to
  # the Steam FHS env. Only helps if the failing binary runs in the outer FHS,
  # not deep inside the pressure-vessel container — if a game still fails in the
  # SLR sandbox, force Proton on it instead.
  programs.steam.extraPackages = [ pkgs.libselinux ];
  services.displayManager.sessionPackages = [
    # Debug variant of sway: logs sway -d output to /tmp/sway-session.log, since
    # a failed session Exec dies silently under GDM (~60s, zero journal output).
    # Remove once sway on the seat is confirmed working.
    (
      (pkgs.writeTextDir "share/wayland-sessions/sway-debug.desktop" ''
        [Desktop Entry]
        Name=Sway (debug)
        Comment=sway -d with logging to /tmp/sway-session.log
        Exec=${pkgs.writeShellScript "sway-debug" ''
          exec > /tmp/sway-session.log 2>&1
          set -x
          date
          id
          echo "seat=$XDG_SEAT vt=$XDG_VTNR session=$XDG_SESSION_ID"
          ls -l /dev/dri/by-path/
          export WLR_NO_HARDWARE_CURSORS=1
          exec sway -d
        ''}
        Type=Application
      '').overrideAttrs
      (_: {
        passthru.providedSessions = [ "sway-debug" ];
      })
    )
  ];

  # Game streaming host: Moonlight client on atlas connects here; games render
  # + NVENC-encode on a 5090. capSysAdmin for KMS capture under Wayland.
  # See <debug/atlas/gpu-strategy.md>.
  services.sunshine = {
    enable = true;
    # Default package has no CUDA → only software x264. NVENC on the 5090s
    # needs the CUDA build.
    package = pkgs.sunshine.override { cudaSupport = true; };
    capSysAdmin = true;
    openFirewall = true;
  };
  # Rule 1: upstream's 60-sunshine.rules — the nixpkgs sunshine package ships no
  # udev rules, so the module's `services.udev.packages = [ package ]` is a no-op
  # and Sunshine gets "Permission denied" creating virtual keyboard/mouse.
  # Rules 2+3: seat0 single-display — hide the virtio-gpu (00:01.0, SPICE console)
  # and the spare 5090 (02:00.0, headless compute) from mutter so seat0 renders
  # only on the display 5090 (01:00.0). This replaces the old seatphysical/seatspare
  # multi-seat rules: multi-GPU mutter with NVIDIA SIGSEGV'd as primary / rendered
  # black as copy target (2026-07-02), so mutter must see exactly one card. Render
  # nodes are not seat- or ignore-gated, so Sunshine / game render-offload still
  # reaches the spare GPU. The TEX Shura keyboard (04d9:0532) needs no rule now — it
  # just defaults to seat0.
  # CAVEAT: `mutter-device-ignore` is honoured by MUTTER ONLY — wlroots/sway ignores
  # it and would grab all three cards (that's why seat0 defaults to GNOME above).
  # virtio stays a plain text/recovery VT console. Gotcha: udev TAGS persist in the
  # udev db — a VM reboot is needed to fully apply a change here.
  # Rule 4: MT7921U Wi-Fi stick (CPAP ez Share sync) — disable USB runtime PM.
  # On 2026-07-25 the stick failed a USB-autosuspend resume (`mt7921u ... resume
  # error -110`), went electrically dark, and stayed dark through four warm
  # reboots (VBUS holds through reboot); only a physical replug at the
  # hypervisor revived it, and cpap-sync was down the whole week. Keeping the
  # device out of autosuspend removes the resume path entirely.
  services.udev.extraRules = ''
    KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"
    SUBSYSTEM=="drm", KERNEL=="card[0-9]*", KERNELS=="0000:00:01.0", TAG+="mutter-device-ignore"
    SUBSYSTEM=="drm", KERNEL=="card[0-9]*", KERNELS=="0000:02:00.0", TAG+="mutter-device-ignore"
    ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="0e8d", ATTR{idProduct}=="7961", TEST=="power/control", ATTR{power/control}="on"
  '';
  # Composite the cursor into frames instead of the hardware cursor plane —
  # Sunshine's KMS capture can't grab the cursor plane on virtio-gpu, so the
  # Moonlight stream has no cursor otherwise.
  environment.sessionVariables.MUTTER_DEBUG_DISABLE_HW_CURSORS = "1";

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
  # virtio4=/dev/vde, virtio5=/dev/vdf, virtio7=/dev/vdh, virtio8=/dev/vdi
  fileSystems."/var/local-path-provisioner" = {
    device = "/dev/vda";
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
  };
  # /dev/vdb (virtio1): 500GB SSD (local-zfs) — Steam library for the gaming seat.
  # Repurposed from the decommissioned Longhorn disk. Games + Proton prefixes must be
  # on SSD: the small-file prefix I/O crawls on the tank-hdd virtiofs share
  # (/mnt/tankshare). See debug/atlas/direct_display_bringup/README.md.
  fileSystems."/games" = {
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
  fileSystems."/var/lib/colibri" = {
    device = "/dev/vdi"; # 500G SSD (local-zfs) — disk-streamed model storage
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

  # A single `systemd.services` assignment: NixOS merges module definitions
  # across files, but two `systemd.services` keys in ONE attrset (the whole-attr
  # `=` here plus a dotted `systemd.services.<x> =` elsewhere) is a Nix-level
  # "attribute already defined" error — so mkMerge the fixed services in here.
  systemd.services = lib.mkMerge [
    # OpenEBS LVM volume groups — idempotent oneshot services that create PV + VG.
    #   openebs-proxmox-ssd: virtio2 (/dev/vdc) — 500GB NVMe (local-zfs)
    #   openebs-proxmox-hdd: virtio6 (/dev/vdg) — 500GB HDD (tank-hdd)
    (lib.mapAttrs'
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
      }
    )
  ];

  # Intermediate directories for the nested repo cache mount.
  # The SSD disk is mounted at ~/.cache/bazel, then the HDD disk is mounted
  # over the repo cache subdirectory inside it.
  systemd.tmpfiles.rules = [
    "d /home/agentydragon/.cache/bazel 0755 agentydragon users -"
    "d /home/agentydragon/.cache/bazel/_bazel_agentydragon 0755 agentydragon users -"
    "d /home/agentydragon/.cache/bazel/_bazel_agentydragon/cache 0755 agentydragon users -"
    "d /home/agentydragon/.cache/bazel/_bazel_agentydragon/cache/repos 0755 agentydragon users -"
    "d /tmp 1777 root root -"
    # Steam library mount (/dev/vdb) must be user-writable; the fresh ext4 root
    # is created root:root, so chown it after the mount lands.
    "d /games 0755 agentydragon users -"
    # The Colibri host experiment runs as agentydragon and stores only
    # reproducible model artifacts on this dedicated SSD.
    "d /var/lib/colibri 0755 agentydragon users -"
    # gnome-remote-desktop system daemon reads $XDG_DATA_HOME/gnome-remote-desktop/grd.conf
    # (= /var/lib/gnome-remote-desktop/.local/share/gnome-remote-desktop/grd.conf); symlink
    # grdConf there so it enables RDP at startup. tmpfiles runs before graphical.target,
    # so the link is in place before the daemon starts.
    "d /var/lib/gnome-remote-desktop/.local/share/gnome-remote-desktop 0755 gnome-remote-desktop gnome-remote-desktop -"
    "L+ /var/lib/gnome-remote-desktop/.local/share/gnome-remote-desktop/grd.conf - - - - ${grdConf}"
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
