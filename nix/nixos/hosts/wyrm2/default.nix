# Wyrm2 - NixOS dev workstation VM + k8s worker
# Similar to rugged (Dell Rugged tablet) but for Proxmox VM.
# Joins the Talos k8s cluster via Nebula mesh.
{
  config,
  pkgs,
  lib,
  username,
  inputs,
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
  # Verbose GDM logging while debugging the seat-game session launch
  # (sessions die without their Exec ever running). Remove when solved.
  services.displayManager.gdm.debug = true;
  # Greeter must never blank/DPMS-off: a dark DP output makes the FV43U's KVM
  # auto-revert to USB-C before the seat-game keyboard can wake anything
  # (see debug/atlas/direct_display_bringup.md).
  programs.dconf.profiles.gdm.databases = [
    {
      settings."org/gnome/desktop/session".idle-delay = lib.gvariant.mkUint32 0;
      # No greeter animations: the seat-game greeter's login fade stalls
      # (frame clock hangs → the animation-completion callback that calls
      # StartSession never fires → "frozen greeter" instead of session
      # start). With animations off the callback runs immediately.
      settings."org/gnome/desktop/interface".enable-animations = false;
    }
  ];

  # gamescope 3.16.17 (nixpkgs 25.11) SIGABRTs on startup when a seat input
  # event races wlserver_init's lock — upstream ValveSoftware/gamescope#1746,
  # fixed in 3.16.24 by PR #2023 ("Lock wlserver while initializing wayland").
  # Pull the fixed build from unstable until 25.11 catches up. Overriding the
  # package-set attr covers the gamescopeSession, the capSysNice wrapper, and
  # Steam in one place. CLEANUP: drop once nixpkgs 25.11 ships gamescope ≥3.16.24.
  nixpkgs.overlays = [
    (_: _: {
      inherit
        (import inputs.nixpkgs-unstable {
          inherit (pkgs.stdenv.hostPlatform) system;
          config.allowUnfree = true;
        })
        gamescope
        ;
    })
  ];

  # Sway session for the game seat (seat-game, on the display 5090). A real WM to
  # game + debug from, run as agentydragon — non-GNOME, so it doesn't clash with
  # the seat0 SPICE GNOME session on the shared user bus (GNOME's fixed D-Bus
  # names are the reason a second GNOME can't run for one user; sway has none).
  # Games get direct scan-out via per-title gamescope (Steam launch option
  # `gamescope -f -- %command%`), not a gamescope kiosk session.
  # NVIDIA: --unsupported-gpu is mandatory; hardware cursors off (wlroots can't
  # do them on this NVIDIA path). On seat-game logind hands sway only the 5090,
  # so no manual WLR_DRM_DEVICES pinning is needed (unlike gamescope).
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
    ];
  };
  # NVIDIA: wlroots can't do hardware cursors on this path. GPU selection is left
  # to the seat assignment — seat-game owns only card2 (the display 5090), so
  # libseat hands sway the right device. (Don't set WLR_DRM_DEVICES to the
  # by-path node: it's a colon-separated list, and the PCI address's colons make
  # wlroots split it into garbage — "Found 0 GPUs".)
  environment.sessionVariables.WLR_NO_HARDWARE_CURSORS = "1";

  # Steam — games run on the RTX 5090s (direct display via seat-game, or
  # streamed to atlas via Sunshine/Moonlight).
  programs.steam.enable = true;
  # "Steam (gamescope)" session in the greeter — the intended session for
  # seat-game: gamescope owns the display 5090's KMS directly (no desktop
  # compositor between game and monitor).
  programs.steam.gamescopeSession = {
    enable = true;
    # Device-selection quirk exploitation (gamescope 3.16.17,
    # src/rendervulkan.cpp): gamescope picks the FIRST Vulkan device unless
    # --prefer-vk-device matches, in which case the LAST matching device
    # wins. Both 5090s match 10de:2b85, Vulkan enumerates in PCI order
    # (01:00.0 compute, 02:00.0 display) → this selects 02:00.0, the
    # display GPU, whose primary DRM node gamescope then opens for KMS
    # (it derives the node from the Vulkan device; WLR_DRM_DEVICES is
    # ignored). Without this it opens the seat0-owned compute card and
    # dies on logind's TakeDevice denial.
    args = [
      "--prefer-vk-device"
      "10de:2b85"
      # NVIDIA direct scan-out produced flicker, corruption, and wrong
      # colors (verified live 2026-07-03: `gamescopectl composite_force 1`
      # fixed all three; gamescope-internal screenshots were always clean).
      "--force-composition"
    ];
  };
  # Let gamescope renice itself for smoother frame pacing (the session UI felt
  # laggy without it). The module installs the cap_sys_nice-wrapped gamescope at
  # /run/wrappers/bin/gamescope; the steam-gamescope session script picks it up
  # from PATH, and the steam module auto-adds the setuid bwrap wrapper this needs.
  programs.gamescope.capSysNice = true;
  # Attempt at the native Steam Linux Runtime `libselinux.so.1` failure (Stellaris
  # dies: "mkdir: libselinux.so.1: cannot open shared object file"). Adds it to
  # the Steam FHS env. Only helps if the failing binary runs in the outer FHS,
  # not deep inside the pressure-vessel container — if a game still fails in the
  # SLR sandbox, force Proton on it instead.
  programs.steam.extraPackages = [ pkgs.libselinux ];
  # Debug variant: same payload but with stdout/stderr + xtrace captured to
  # /tmp/steam-session.log — the stock session dies silently under GDM
  # (~60s, zero journal output). Remove once the session works.
  services.displayManager.sessionPackages = [
    (
      (pkgs.writeTextDir "share/wayland-sessions/steam-debug.desktop" ''
        [Desktop Entry]
        Name=Steam (debug)
        Comment=steam-gamescope with logging to /tmp/steam-session.log
        Exec=${pkgs.writeShellScript "steam-gamescope-debug" ''
          exec > /tmp/steam-session.log 2>&1
          set -x
          date
          id
          # Restrict wlroots/gamescope DRM discovery to the display 5090:
          # gamescope naively opens the first card node (card0 = the compute
          # GPU, owned by seat0) and dies when logind refuses it.
          export WLR_DRM_DEVICES=/dev/dri/by-path/pci-0000:02:00.0-card
          exec steam-gamescope
        ''}
        Type=Application
      '').overrideAttrs
      (_: {
        passthru.providedSessions = [ "steam-debug" ];
      })
    )
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
  # Rule 1: upstream's 60-sunshine.rules — the nixpkgs sunshine package ships
  # no udev rules, so the module's `services.udev.packages = [ package ]` is
  # a no-op and Sunshine gets "Permission denied" creating virtual
  # keyboard/mouse.
  # Rules 2+3: multiseat for the direct-display gaming plan — see
  # debug/atlas/direct_display_bringup.md.
  # - The display 5090 (guest 02:00.0 = hostpci1; DP cable to the FV43U)
  #   belongs to logind seat "seat-game": GDM spawns a separate greeter
  #   there, independent of the seat0 SPICE desktop. Do NOT
  #   mutter-device-ignore this card — the seat-game greeter must use it.
  # - The other 5090 (01:00.0, headless compute) stays on seat0 but is
  #   hidden from mutter: multi-GPU mutter with NVIDIA cards crash-looped
  #   (SIGSEGV) as primary and rendered black as copy target (2026-07-02).
  # Gotcha: udev TAGS persist in the udev db — removing/changing these
  # rules needs a VM reboot to fully take effect.
  services.udev.extraRules = ''
    KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"
    SUBSYSTEM=="drm", KERNEL=="card[0-9]*", KERNELS=="0000:01:00.0", TAG+="mutter-device-ignore"
  '';
  # seat-game device assignments must run at priority 72 — BEFORE systemd's
  # 73-seat-late.rules finalizes seat bookkeeping (extraRules lands in
  # 99-local.rules, which is too late: libinput saw ID_SEAT there but logind
  # denied TakeDevice to the seat-game greeter). Same slot loginctl-attach
  # uses. Devices: the display 5090 (guest 02:00.0), and the TEX Shura
  # (04d9:0532), which arrives via QEMU port-path passthrough ONLY when the
  # monitor's KVM routes its hub to USB-B — in this guest it is unambiguously
  # the seat-game keyboard. See debug/atlas/direct_display_bringup.md.
  services.udev.packages = [
    (pkgs.writeTextFile {
      name = "seat-game-udev-rules";
      destination = "/lib/udev/rules.d/72-seat-game.rules";
      text = ''
        SUBSYSTEM=="drm", KERNEL=="card[0-9]*", KERNELS=="0000:02:00.0", ENV{ID_SEAT}="seat-game"
        # No KERNEL=="event*" filter: logind resolves an evdev node's seat via
        # its PARENT input-class device, so inputNN needs ID_SEAT too (event-
        # only assignment = libinput claims it but logind denies TakeDevice).
        SUBSYSTEM=="input", ATTRS{idVendor}=="04d9", ATTRS{idProduct}=="0532", TAG+="seat", ENV{ID_SEAT}="seat-game"
      '';
    })
  ];
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
  # virtio4=/dev/vde, virtio5=/dev/vdf, virtio7=/dev/vdh
  fileSystems."/var/local-path-provisioner" = {
    device = "/dev/vda";
    fsType = "ext4";
    autoFormat = true;
    autoResize = true;
  };
  # /dev/vdb (virtio1): 500GB SSD (local-zfs) — Steam library for the gaming seat.
  # Repurposed from the decommissioned Longhorn disk. Games + Proton prefixes must be
  # on SSD: the small-file prefix I/O crawls on the tank-hdd virtiofs share
  # (/mnt/tankshare). See debug/atlas/direct_display_bringup.md.
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
    # Steam library mount (/dev/vdb) must be user-writable; the fresh ext4 root
    # is created root:root, so chown it after the mount lands.
    "d /games 0755 agentydragon users -"
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
