# Dell Rugged 12 tablet
#
# Manual setup steps:
# - SSH keygen and copy to GitHub
# - Transfer over Ansible Vault password into libwallet
#
# TODO: Consider moving some packages from home-manager to system level (zsh, compilers like rustc/go/gcc)
# TODO: Improved OSK extension - waiting for GNOME 49 support (currently only 43-44)
# TODO: auto-cpufreq - services.auto-cpufreq for dynamic CPU governor (power saving on battery, performance on AC)
# TODO: Vulkan crash on Lunar Lake - Snapshot (and likely other GTK4 apps) segfault with VK_ERROR_DEVICE_LOST.
#   Workaround: GSK_RENDERER=gl. Consider adding to environment.sessionVariables or wrapping affected apps.
# TODO: PipeWire - explicit audio config (services.pipewire with pulse/alsa/jack support)
# TODO: bluetooth group - add to extraGroups if direct bluetooth access needed beyond blueman
{
  config,
  pkgs,
  lib,
  inputs,
  username,
  ...
}:
let
  keys = import ../../../ssh-keys.nix;
in
{
  imports = [
    ../../modules/operator.nix
    ./hardware-configuration.nix
    ../../modules/claude-desktop.nix
    ../../modules/k8s-workstation.nix
    ../../modules/github-api-recorder.nix
    ./ipu7-camera.nix
    ./foxconn-wwan.nix
    ./nebula-underlay-refresh.nix
    ./local_llm_arc.nix
    ./local_llm_router.nix
    ./local_llm_bearer_proxy.nix
    ./local_llm_npu.nix
    ./gpu-debug.nix
    ./iio-debug.nix
    ../../modules/rapl-metrics.nix
    ../../modules/attic-substituter.nix
  ];

  # Pull substituter for cache.allegedly.works/{main,gaffer}. Reader JWT is
  # auto-rotated by attic-jwt-rotation CronJob; the SOPS file is decryptable
  # by the rugged host key + agentydragon user key.
  ducktape.attic-substituter = {
    enable = true;
    sopsFile = ../../../../secrets/hosts/rugged-attic.yaml;
  };

  ducktape.k8sWorker = {
    nodeLabels = {
      "topology.kubernetes.io/region" = "roaming";
      "node.kubernetes.io/role" = "roaming";
      # Scopes the journal-only promtail DaemonSet to systemd/journald nodes
      # (the promtail-journal HelmRelease in cluster/cdk8s/monitoring/loki.py).
      "node-vendor" = "nixos";
    };
    nodeTaints = [ "node-role.kubernetes.io/roaming=true:NoSchedule" ];
  };

  # Attribution for the GitHub GraphQL quota drain; see the module header.
  ducktape.githubApiRecorder.enable = true;

  # IPU7 webcam (Intel Lunar Lake, OV08X40 sensor)
  ducktape.ipu7Camera.enable = true;

  # Claude Desktop Cowork sandboxed-microVM feature (QEMU firmware + virtiofsd
  # at the Debian /usr paths the app probes).
  ducktape.cowork.enable = true;

  # Local LLM inference (Arc GPU + NPU)
  # The router verifies/downloads its pinned model blobs into persistent local
  # storage before starting; existing Ollama blobs are an optional import source.
  ducktape.localLlm.ollamaUpstream.enable = false;
  # Keep these model declarations ready for an explicit Ollama fallback; the
  # active llama.cpp router uses its separate digest-pinned artifact manifest.
  ducktape.localLlm.ollamaUpstream.models = [
    "qwen3:4b-instruct-2507-q4_K_M"
    "gemma4:e2b-it-qat"
    "gemma4:e4b"
    "qwen2.5-coder:7b"
    "qwen3:8b"
    "qwen3.5:0.8b"
  ];
  ducktape.localLlm.ollamaUpstream.aliases."gemma4:e2b-it-qat-long-128k-b128" = {
    from = "gemma4:e2b-it-qat";
    numCtx = 131072;
    numBatch = 128;
  };
  ducktape.localLlm.llamaRouter.enable = true;
  # Allow LiteLLM cluster access through the bearer proxy. OpenCode uses the loopback router directly.
  ducktape.localLlm.bearerProxy.enable = true;

  # Keep the llama.cpp service disabled until its own GGUF models and bounded
  # context are verified. The stock driver does execute NPU work with the
  # correct Level Zero library paths; see the investigation linked below.
  ducktape.localLlm.npu.enable = false;

  # NPU kernel/userspace driver and firmware only, so Level Zero and OpenVINO
  # can be tested on the device without the LLM service. Doc:
  # cluster/docs/inference/rugged_npu_driver.md
  hardware.cpu.intel.npu.enable = true;

  # Separate btrfs subvolume for containerd storage. Create it before first boot with:
  #   sudo mount -t btrfs /dev/mapper/cryptroot /mnt/btrfs-root
  #   sudo btrfs subvolume create /mnt/btrfs-root/@containerd
  #   sudo umount /mnt/btrfs-root
  fileSystems."/var/lib/containerd" = {
    device = "/dev/mapper/cryptroot";
    fsType = "btrfs";
    options = [ "subvol=@containerd" ];
  };

  # Piraeus `ssd` pool: its own LUKS partition behind the shrunk root partition,
  # unlocked after root is mounted by a keyfile on the encrypted root (never in
  # the initrd, which sits unencrypted on the ESP). LINSTOR initializes the opened
  # device as its LVM PV. `ducktape.piraeusHost` stays off: its DRBD 9.2.16 fails
  # to build against this host's 7.2 kernel (`struct sockaddr_unsized`, 2026-10-27).
  environment.etc.crypttab.text = ''
    piraeus-ssd /dev/disk/by-partlabel/rugged-piraeus /etc/keys/rugged-piraeus.key luks,nofail,x-systemd.device-timeout=10s
  '';

  # Bluetooth
  hardware.bluetooth = {
    enable = true;
    powerOnBoot = true;
  };
  # IIO sensor proxy for accelerometer (auto screen rotation)
  hardware.sensor.iio.enable = true;

  # High-volume IIO debugging is useful only during an active auto-rotate
  # investigation. Leaving it enabled during normal use flooded journald and
  # caused periodic desktop stalls. See nix/debug/rugged/stalls/report.md.
  ducktape.iioDebug.enable = false;

  # The Intel RAPL counters exist but are root-only on this Lunar Lake host.
  # Keep node-exporter non-root and grant it only read access to energy_uj.
  ducktape.raplMetrics.enable = true;

  zramSwap.enable = true;
  systemd.oomd.enableUserSlices = true;

  # Add Plasma as a parallel Wayland session for tablet/OSK testing while
  # keeping the existing GNOME session and GDM display manager intact.
  # See nix/debug/rugged/osk_window_avoidance/report.md for the OSK investigation
  # that motivates this temporary KDE probe.
  services.desktopManager.plasma6.enable = true;
  services.displayManager.defaultSession = "gnome";
  programs.ssh.askPassword = lib.mkForce "${pkgs.seahorse}/libexec/seahorse/ssh-askpass";

  services = {
    avahi = {
      enable = true;
      nssmdns4 = true; # mDNS resolution for .local hostnames (printers, etc.)
    };
    blueman.enable = true;
    fwupd.enable = true;
    printing.enable = true;
    thermald.enable = true; # Intel thermal management
    upower.enable = true; # Battery status (dual battery support)
    logind = {
      settings.Login = {
        KillUserProcesses = false; # Keep tmux alive across GNOME logout/login
        HandleLidSwitch = "suspend";
        HandleLidSwitchExternalPower = "lock";
        HandlePowerKey = "suspend";
        HandlePowerKeyLongPress = "poweroff";
      };
    };
  };

  # WWAN/5G modem support (Foxconn DP25-42843-47)
  networking.modemmanager.enable = true;
  ducktape.foxconnWwan = {
    enable = true;
    # Temporarily capture MHI suspend returns and resource counters.
    # Revert after resolving https://github.com/agentydragon/ducktape/issues/11789.
    debugMhiSuspend = true;
  };

  # Native Wayland for Chrome and Electron apps. Without this, they run under
  # XWayland and can't use the PipeWire camera portal (or screen sharing portal).
  # TODO: Check if NIXOS_OZONE_WL is actually needed for PipeWire camera, or if
  #   WebRtcPipeWireCamera alone suffices (the portal is D-Bus, not display-tied).
  #   We only tested both together.
  environment.sessionVariables.NIXOS_OZONE_WL = "1";

  # Enable PipeWire camera portal in Chrome. IPU7 raw V4L2 nodes are non-functional
  # (the ISP pipeline requires libcamera), so Chrome must use the PipeWire camera
  # source via xdg-desktop-portal instead of enumerating /dev/video* directly.
  # TODO: Check if WebRtcPipeWireCamera alone is enough, or if NIXOS_OZONE_WL is
  #   also required. We only tested both together.
  nixpkgs.overlays = [
    (_final: prev: {
      google-chrome = prev.google-chrome.override {
        # WebRtcPipeWireCamera: IPU7 camera requires PipeWire portal (can't enumerate /dev/video* directly)
        # disable-quic: Google Fi carrier blocks UDP, causing QUIC handshake timeouts before TCP fallback
        commandLineArgs = "--enable-features=WebRtcPipeWireCamera --disable-quic";
      };

      # Host-local Mutter patch for the auto-rotate startup race on this tablet.
      mutter = prev.mutter.overrideAttrs (old: {
        patches = (old.patches or [ ]) ++ [
          ./mutter-auto-rotate-startup-race.patch
        ];
      });
    })
  ];

  hardware.enableAllFirmware = true;

  # System packages
  environment.systemPackages = with pkgs; [
    kdePackages.plasma-keyboard # Plasma virtual keyboard for OSK window-avoidance testing
    maliit-framework # Alternative Wayland input-method stack for comparison
    maliit-keyboard
    evtest # Input device event inspection for tablet/stylus debugging
    powertop
    snapshot # GNOME camera app (uses libcamera/PipeWire natively)
    telegram-desktop
    xdg-terminal-exec # Used by custom Ctrl+Alt+T keybinding; configure via xdg-terminals.list
    zoom-us

    # WWAN / eSIM management
    lpac # eUICC/eSIM profile management (lpac profile list/download/enable)
    libmbim # mbimcli for MBIM modem queries (signal, UICC, registration)
    libqmi # qmicli for QMI-over-MBIM queries (UIM card status)
  ];

  # Local file sharing across devices (LAN)
  programs.localsend = {
    enable = true;
    openFirewall = true;
  };

  programs.steam.enable = true;

  users.users.${username} = {
    shell = pkgs.zsh;
    # Allow reading system logs without sudo (systemd-journal group)
    # render: /dev/accel/* access for the NPU
    extraGroups = [
      "systemd-journal"
      "render"
    ];
    openssh.authorizedKeys.keys = with keys; [
      iguana
      wyrm2
      atlas
      ruggedMcpAgentydragon
    ];
  };

  users.users.root.openssh.authorizedKeys.keys = [ keys.ruggedMcpRoot ];
  services.openssh.settings.PermitRootLogin = lib.mkForce "prohibit-password";

  # SPICE USB redirection helper (setuid root for USB device passthrough)
  security.wrappers.spice-client-glib-usb-acl-helper = {
    setuid = true;
    owner = "root";
    group = "root";
    source = "${pkgs.spice-gtk}/bin/spice-client-glib-usb-acl-helper";
  };

  # User groups provided by base.nix: wheel, networkmanager, video, audio
}
