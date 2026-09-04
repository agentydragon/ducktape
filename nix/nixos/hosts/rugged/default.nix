# Dell Rugged 12 tablet
#
# Manual setup steps:
# - SSH keygen and copy to GitHub
# - Transfer over Ansible Vault password into libwallet
#
# TODO: Consider moving some packages from home-manager to system level (zsh, compilers like rustc/go/gcc)
# TODO: SSH authorized_keys - add keys to openssh.authorizedKeys.keys
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
    ./hardware-configuration.nix
    ../../modules/gui.nix
    ../../modules/workstation.nix
    ../../modules/bazel
    ../../modules/claude-desktop.nix
    ../../modules/system-inspection-sudo.nix
    ../../modules/k8s-worker.nix
    ../../modules/hostexecd.nix
    ../../modules/github-api-recorder.nix
    ./ipu7-camera.nix
    ./foxconn-wwan.nix
    ./nebula-underlay-refresh.nix
    ./local_llm_arc.nix
    ./local_llm_npu.nix
    ./gpu-debug.nix
    ./iio-debug.nix
    ../../modules/attic-substituter.nix
  ];

  # Pull substituter for cache.allegedly.works/{main,gaffer}. Reader JWT is
  # auto-rotated by attic-jwt-rotation CronJob; the SOPS file is decryptable
  # by the rugged host key + agentydragon user key.
  ducktape.attic-substituter = {
    enable = true;
    sopsFile = ../../../../secrets/hosts/rugged-attic.yaml;
  };

  # Passwordless sudo for system inspection commands
  ducktape.systemInspectionSudo.enable = true;

  ducktape.k8sWorker = {
    enable = true;
    nodeLabels = {
      "topology.kubernetes.io/region" = "roaming";
      "node.kubernetes.io/role" = "roaming";
      # Scopes the journal-only promtail DaemonSet to systemd/journald nodes
      # (cluster/k8s/monitoring/loki/promtail-journal-helmrelease.yaml).
      "node-vendor" = "nixos";
    };
    nodeTaints = [ "node-role.kubernetes.io/roaming=true:NoSchedule" ];
  };

  # hostexecd: haku-console runs approved commands here under the operator's own
  # Authentik identity and outbound daemon id derive from networking.hostName.
  # See nix/nixos/modules/hostexecd.nix.
  ducktape.hostexec.enable = true;

  # Attribution for the GitHub GraphQL quota drain; see the module header.
  ducktape.githubApiRecorder.enable = true;

  # IPU7 webcam (Intel Lunar Lake, OV08X40 sensor)
  ducktape.ipu7Camera.enable = true;

  # Claude Desktop Cowork sandboxed-microVM feature (QEMU firmware + virtiofsd
  # at the Debian /usr paths the app probes).
  ducktape.cowork.enable = true;

  # Local LLM inference (Arc GPU + NPU)
  ducktape.localLlm.arc.enable = true;
  ducktape.localLlm.npu.enable = true;
  ducktape.localLlm.ollamaUpstream.enable = true;

  # Separate btrfs subvolumes for containerd and local-path-provisioner storage.
  # Create them before first boot with:
  #   sudo mount -t btrfs /dev/mapper/cryptroot /mnt/btrfs-root
  #   sudo btrfs subvolume create /mnt/btrfs-root/@containerd
  #   sudo btrfs subvolume create /mnt/btrfs-root/@local-path-provisioner
  #   sudo umount /mnt/btrfs-root
  fileSystems."/var/lib/containerd" = {
    device = "/dev/mapper/cryptroot";
    fsType = "btrfs";
    options = [ "subvol=@containerd" ];
  };
  fileSystems."/var/local-path-provisioner" = {
    device = "/dev/mapper/cryptroot";
    fsType = "btrfs";
    options = [ "subvol=@local-path-provisioner" ];
  };

  # Bluetooth
  hardware.bluetooth = {
    enable = true;
    powerOnBoot = true;
  };
  # IIO sensor proxy for accelerometer (auto screen rotation)
  hardware.sensor.iio.enable = true;

  # High-volume IIO debugging is useful only during an active auto-rotate
  # investigation. Leaving it enabled during normal use flooded journald and
  # caused periodic desktop stalls. See debug/rugged/stalls/report.md.
  ducktape.iioDebug.enable = false;

  # Prefer compressed RAM over the disk swapfile for interactive use. The
  # existing disk swap remains available as a lower-priority fallback.
  zramSwap = {
    enable = true;
    algorithm = "zstd";
    memoryPercent = 50;
    priority = 100;
  };
  boot.kernel.sysctl."vm.swappiness" = 10;

  # Add Plasma as a parallel Wayland session for tablet/OSK testing while
  # keeping the existing GNOME session and GDM display manager intact.
  # See debug/rugged/osk_window_avoidance/report.md for the OSK investigation
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
  ducktape.foxconnWwan.enable = true;

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
    extraGroups = [ "systemd-journal" ];
    openssh.authorizedKeys.keys = with keys; [
      iguana
      wyrm2
      atlas
    ];
  };

  # SPICE USB redirection helper (setuid root for USB device passthrough)
  security.wrappers.spice-client-glib-usb-acl-helper = {
    setuid = true;
    owner = "root";
    group = "root";
    source = "${pkgs.spice-gtk}/bin/spice-client-glib-usb-acl-helper";
  };

  # User groups provided by base.nix: wheel, networkmanager, video, audio
}
