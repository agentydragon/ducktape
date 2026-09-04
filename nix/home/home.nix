{
  config,
  pkgs,
  pkgsUnstable,
  lib,
  enableGui,

  isNixOS,
  isK8sWorker,
  ducktape-artifacts,
  ...
}:
let
  toTOML = (pkgs.formats.toml { }).generate;

  # `codex-claude`: Claude Code on ChatGPT/Codex via the in-cluster CLIProxyAPI gateway.
  # See ./claude_code/codex-claude.nix.
  codexClaude = import ./claude_code/codex-claude.nix { inherit pkgs; };

  ducktapePackages = import ../packages {
    inherit lib pkgs;
    artifacts = ducktape-artifacts;
  };
  # The nixos-26.05 channel still packages the October 2025 Pop Shell snapshot,
  # which only declares support through GNOME 49. The already-pinned unstable
  # package carries upstream's GNOME 50 support; keep release packages on older
  # GNOME hosts so this compatibility override is no broader than necessary.
  popShellPackage =
    if lib.versionAtLeast pkgs.gnome-shell.version "50" then
      pkgsUnstable.gnomeExtensions.pop-shell
    else
      pkgs.gnomeExtensions.pop-shell;
  inherit (ducktapePackages)
    ducktape
    claude-hook-rs
    claude-statusline
    gterm-theme
    bbapi
    ;

  tanaClaude = import ./claude_code/tana-claude.nix { inherit pkgs; };

  # `gemini-claude`: Claude Code on Google Gemini via the cluster LiteLLM proxy, reading
  # $GEMINI_LITELLM_KEY. See ./claude_code/gemini-claude.nix.
  geminiClaude = import ./claude_code/gemini-claude.nix { inherit pkgs; };

  mkHomeGtkBookmark =
    { path, title }:
    "file://${config.home.homeDirectory}/${path} ${title}";

  gtkFileChooserBookmarks = map mkHomeGtkBookmark (
    [
      {
        path = "code/ducktape";
        title = "Ducktape";
      }
    ]
    ++ lib.optionals config.services.google-drive.enable [
      {
        path = "drive";
        title = "Google Drive";
      }
      {
        path = "drive/dokumenty";
        title = "Dokumenty";
      }
    ]
  );
in
{
  _module.args.ducktapePackages = ducktapePackages;

  imports = [
    ./codex
    ./crush
    ./modules/neovim.nix
    ./modules/shell.nix
    ./modules/tmux.nix
    ./modules/solarized.nix
    ./terminals
    ./claude_code
    # TODO(gemini-cli): Gemini CLI integration disabled (unused). home-manager
    # 26.05 renamed `programs.gemini-cli` → `programs.antigravity-cli` (Google
    # rebrand) via mkRenamedOptionModule, and `antigravity-cli` is only in
    # nixpkgs-unstable, not the nixos-26.05 release wyrm2 pins — so setting
    # programs.gemini-cli forces `pkgs.antigravity-cli` and fails eval. Never used
    # it. Later: either migrate the custom module + personal integration to
    # programs.antigravity-cli (once the package is in a release) or delete them.
    # The generic module + personal integration files stay in the tree meanwhile.
    # ./programs/gemini-cli.nix
    # ./gemini_cli.nix
    ./shell/oh-my-posh.nix
    ./modules/gnome-custom-keybindings.nix
    ./modules/attic.nix
    ./modules/atuin.nix
    ./modules/buildbuddy.nix
    ./modules/github-api-proxy.nix
    ./modules/sops-env.nix
    ./services/activitywatch.nix
    ./opencode
    ./modules/gnome-shell-keybindings.nix
    ./modules/flameshot-screenshots.nix
    ./modules/aiquota.nix
    ./modules/haku-approvals.nix
  ];
  # Workstations use the cluster/local (gpt-oss) Codex model providers + profiles.
  ducktape.codex.localModels.enable = true;
  ducktape.sopsEnv = {
    HF_TOKEN = {
      sopsFile = ../../secrets/shared/huggingface.yaml;
      key = "hf_token";
    };
    HABITIFY_API_KEY = {
      sopsFile = ../../secrets/shared/habitify.yaml;
      key = "habitify_api_key";
    };
    # The same ciphertext the agent egress proxies consume in-cluster, read
    # here directly rather than copied: see .sops.yaml for why this one
    # cluster/k8s file is also encrypted to the workstation keys.
    BRAVE_API_KEY = {
      sopsFile = ../../cluster/k8s/agents/shared-secrets/brave-search-api-key.sops.yaml;
      key = "stringData/api-key";
      name = "brave_api_key";
    };
    # Tana-scoped LiteLLM virtual key powering the `tana-claude` alias below.
    TANA_LITELLM_KEY = {
      sopsFile = ../../tf/gitops/litellm-keys/litellm-tana-clients-key.yaml;
      key = "litellm_tana_key";
    };
    # Codex-scoped LiteLLM virtual key powering the `codex-claude` alias below
    # (LiteLLM → CLIProxyAPI). The cli-proxy client key itself stays in-cluster only.
    CODEX_LITELLM_KEY = {
      sopsFile = ../../tf/gitops/litellm-keys/litellm-codex-clients-key.yaml;
      key = "litellm_codex_key";
    };
    # Gemini-scoped LiteLLM virtual key powering the `gemini-claude` alias below
    # (LiteLLM → Google Gemini). The upstream GEMINI_API_KEY stays in-cluster only.
    GEMINI_LITELLM_KEY = {
      sopsFile = ../../tf/gitops/litellm-keys/litellm-gemini-clients-key.yaml;
      key = "litellm_gemini_key";
    };
  };

  home.username = "agentydragon";
  home.homeDirectory = "/home/agentydragon";

  programs.home-manager.enable = true;

  xdg.userDirs = {
    enable = true;
    createDirectories = false;
    # Pin the pre-26.05 default (silence the HM 26.05 default-flip warning;
    # stateVersion already implies true).
    setSessionVariables = true;
    desktop = "$HOME";
    documents = "$HOME";
    download = "$HOME/downloads";
    music = "$HOME";
    pictures = "$HOME";
    publicShare = "$HOME";
    templates = "$HOME";
    videos = "$HOME";
  };

  xdg.configFile."gtk-3.0/bookmarks" = lib.mkIf enableGui {
    text = lib.concatStringsSep "\n" gtkFileChooserBookmarks + "\n";
  };

  nix.package = lib.mkDefault pkgs.nix;

  nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 14d";
  };

  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
      # fetch-closure: user-side eval of nix/packages/gaffer.nix
      # (builtins.fetchClosure, reached via the google-drive HM module) needs
      # this, or `nix build` of google-drive-enabled configs fails with
      # "attribute 'fetchClosure' missing". Mirrors base.nix's system-level
      # setting so /etc/nix/nix.conf and this HM-managed ~/.config/nix/nix.conf
      # agree.
      "fetch-closure"
    ];
    download-buffer-size = 268435456;
    connect-timeout = 5;
    substituters = [
      "https://cache.nixos.org/"
    ];
    trusted-public-keys = [
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
    ];
  };

  programs.git = {
    enable = true;
    package = (pkgs.git.override { withLibsecret = true; }).overrideAttrs {
      doCheck = false;
      doInstallCheck = false;
    };
    lfs.enable = true;

    ignores = [
      ".aider*"
      "__pycache__"
      "*.sw[op]"
      "**/.claude/settings.local.json"
      "**/CLAUDE.local.md"
      "oneoff__*"
    ];

    settings = {
      user = {
        name = "Rai";
        email = "agentydragon@gmail.com";
      };
      core.autocrlf = false;
      color.ui = "auto";
      # `simple` pushes only when the current branch name matches its upstream
      # branch name, otherwise it refuses. This avoids the `upstream` footgun:
      # `git worktree add -b foo ... origin/devel` makes `foo` track
      # `origin/devel`, so a plain push from `foo` can update `devel` instead
      # of creating/pushing `foo`.
      push.default = "simple";
      log = {
        abbrevCommit = true;
        decorate = "short";
        date = "local";
      };
      format.pretty = "short";
      advice = {
        pushNonFastForward = false;
        statusHints = false;
        commitBeforeMerge = false;
      };
      clean.requireForce = true;
      branch.autosetuprebase = "always";
      rebase.autostash = true;
      rebase.forkPoint = false;
      rerere.enabled = true;
      init.defaultBranch = "main";
      merge.tool = "vimdiff";
      credential.helper = "libsecret";
      "url \"git@github.com:\"" = {
        insteadOf = [
          "https://github.com"
          "https://github.com/"
        ];
      };
      "difftool \"nbdime\"".cmd = "git-nbdifftool diff \"$LOCAL\" \"$REMOTE\" \"$BASE\"";
      difftool.prompt = false;
      "mergetool \"nbdime\"".cmd = "git-nbmergetool merge \"$BASE\" \"$LOCAL\" \"$REMOTE\" \"$MERGED\"";
      mergetool.prompt = false;
    };
  };

  programs.gpg = {
    enable = true;
    settings = {
      use-agent = true;
      default-preference-list = "SHA512 SHA384 SHA256 AES256 AES192 AES ZLIB BZIP2 ZIP Uncompressed";
      personal-cipher-preferences = "AES256 AES192 AES";
      personal-digest-preferences = "SHA512 SHA384 SHA256";
      fixed-list-mode = true;
      keyid-format = "0xlong";
      with-fingerprint = true;
    };
  };

  services.gpg-agent = {
    enable = true;
    defaultCacheTtl = 28800;
    maxCacheTtl = 86400;
    pinentry.package = pkgs.pinentry-gtk2;
  };

  services.ssh-agent.enable = true;

  sops.age.sshKeyPaths = [ "${config.home.homeDirectory}/.ssh/id_ed25519" ];

  programs.ssh = {
    enable = true;
    enableDefaultConfig = false;
    settings = {
      # agent-box VM: `ssh agent-box.allegedly.works` lands as the codex user.
      # Distinct port because gecko owns :22 on the hil nodes (see
      # cluster/k8s/agent-box/app/ciliumenvoyconfig.yaml).
      "agent-box.allegedly.works" = {
        HostName = "agent-box.allegedly.works";
        User = "codex";
        Port = 2201;
      };
      # codex-pod (k8s image pod): no exposed port — tunnel to its 127.0.0.1 sshd
      # through `kubectl exec` + a socat relay. kube RBAC gates the transport; the
      # pod's sshd adds pubkey auth so ssh-native tools (rsync/scp/git/VS Code
      # Remote) work. Host key isn't verified (exec-gated, and the PVC host key can
      # be reprovisioned). Needs a cluster kubeconfig on PATH.
      "codex-pod" = {
        User = "codex";
        IdentityFile = "~/.ssh/id_ed25519";
        ProxyCommand = "kubectl exec -i -n codex-pod deploy/codex-pod -c codex -- socat - TCP:127.0.0.1:2222";
        StrictHostKeyChecking = "no";
        UserKnownHostsFile = "/dev/null";
      };
    };
  };
  xdg.configFile."appimagelauncher.cfg".text = ''
    [AppImageLauncher]
    %23%20%23%20additional_directories_to_watch=~/otherApplications:/even/more/applications
    %23%20%23%20monitor_mounted_filesystems=false
    ask_to_move=true
    destination=/home/agentydragon/.local/appimages
    enable_daemon=true
  '';

  home.file.".bazelrc".text = ''
    common --show_progress_rate_limit=0.05
    common --progress_in_terminal_title

    try-import ${config.home.homeDirectory}/.config/bazel/buildbuddy.bazelrc
  '';

  home.packages =
    with pkgs;
    [
      (python3.withPackages (
        ps: with ps; [
          pydeps
        ]
      ))

      pkgs.pyright

      ast-grep
      attic-client
      awscli2
      gnuplot
      jq
      mc
      mmv
      nethogs
      speedtest-cli
      uv
      xxd
      yq
      zsh
      atuin

      gh
      glab
      gitstatus

      nodejs_24
      pnpm
      bun

      rustc
      cargo
      clippy
      rust-analyzer
      sccache
      gcc

      codexClaude
      tanaClaude
      geminiClaude

      go

      direnv
      devenv
      rclone
      pkgsUnstable.opencode

      stylua

      ducktape
      claude-hook-rs
      claude-statusline
      bbapi
      gterm-theme
      ducktapePackages.tana-outliner
    ]
    ++ [
      eza
      zoxide
      fzf
      fd
      tokei

      pwgen
      ffmpeg
      sqlite
      gnupg

      oh-my-posh
      zsh-powerlevel10k

      yt-dlp
      pdftk
      qpdf
    ]
    ++ lib.optionals enableGui [
      nerd-fonts.fira-code
      nerd-fonts.droid-sans-mono
      nerd-fonts.jetbrains-mono
      nerd-fonts.inconsolata
      nerd-fonts.liberation
      nerd-fonts.meslo-lg
      nerd-fonts.profont
      nerd-fonts.ubuntu-mono
      nerd-fonts.hack
      nerd-fonts.sauce-code-pro
      nerd-fonts.iosevka
      nerd-fonts.victor-mono
      nerd-fonts.proggy-clean-tt
      nerd-fonts.caskaydia-cove

      roboto

      # Emoji fallback (✅ U+2705, etc.). Nerd Font patching only adds PUA icons,
      # not Unicode emoji; NixOS+GNOME hosts get this implicitly via
      # fonts.enableDefaultPackages, but standalone home-manager hosts (atlas)
      # have no system emoji font, so declare it here for every GUI host.
      noto-fonts-color-emoji

      # CLEANUP: return to pkgs.flameshot once nixos-25.11 ships v14.0.0 or
      # newer; it fixes GNOME Wayland portal requests with an empty parent.
      pkgsUnstable.flameshot
      xclip

      mplayer
      mpv

      geeqie
      evince

      scrcpy

      anki

      gnome-tweaks
      dconf-editor
    ];

  targets.genericLinux.enable = !isNixOS;

  fonts.fontconfig.enable = enableGui;
  # Pin emoji fallback so charset resolution is deterministic (otherwise
  # fontconfig may land on a monochrome font, e.g. FreeSerif, for U+2705).
  fonts.fontconfig.defaultFonts.emoji = lib.mkIf enableGui [ "Noto Color Emoji" ];

  home.activation.fixMimeApps = lib.mkIf enableGui (
    lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run ${pkgs.xdg-utils}/bin/xdg-mime default google-chrome.desktop text/html
      run ${pkgs.xdg-utils}/bin/xdg-mime default org.gnome.Evince.desktop application/pdf
      run ${pkgs.xdg-utils}/bin/xdg-mime default remote-viewer.desktop application/x-virt-viewer
    ''
  );

  ducktape.gnomeCustomKeybindings.terminal = lib.mkIf enableGui {
    name = "Launch Terminal";
    command = "xdg-terminal-exec";
    binding = "<Primary><Alt>t";
  };

  xdg.configFile."xdg-terminals.list" = lib.mkIf enableGui {
    text = "org.gnome.Terminal.desktop\n";
  };

  xdg.dataFile."themes/Ducktape Shell/gnome-shell/gnome-shell.css" = lib.mkIf enableGui {
    text = ''
      @import url("resource:///org/gnome/shell/theme/gnome-shell.css");

      #panel .panel-button,
      #panel .panel-button:hover,
      #panel .panel-button:active,
      #panel .panel-button:focus,
      #panel .panel-button:checked,
      #panel .panel-button:overview,
      #panel .clock-display,
      #panel .clock-display:hover,
      #panel .clock-display:active,
      #panel .clock-display:focus,
      #panel .clock-display:checked,
      #panel .clock-display:overview,
      #panel .clock-display .clock,
      #panel .clock-display-box {
        border-radius: 4px;
      }
    '';
  };

  xdg.autostart = lib.mkIf enableGui {
    enable = true;
    entries = [
      (pkgs.writeText "syncthing-gtk.desktop" ''
        [Desktop Entry]
        Type=Application
        Name=Syncthing-GTK
        Exec=syncthing-gtk --minimized
        Icon=syncthing-gtk
        Terminal=false
        Categories=Network;FileTransfer;
        X-GNOME-Autostart-enabled=true
      '')
    ];
  };

  dconf = lib.mkIf enableGui {
    enable = true;
    settings = {
      "org/gnome/desktop/wm/preferences" = {
        focus-mode = "sloppy";
        button-layout = ":minimize,maximize,close";
      };

      "org/gnome/settings-daemon/plugins/color" = {
        night-light-enabled = true;
        night-light-temperature = lib.hm.gvariant.mkUint32 2414;
      };

      "org/gnome/settings-daemon/plugins/power" = lib.mkIf isK8sWorker {
        sleep-inactive-ac-type = "nothing";
      };

      "org/gnome/shell/extensions/panel-date-format" = {
        format = "%a %Y-%m-%d %H:%M";
      };

      "org/gnome/shell/extensions/just-perfection" = {
        clock-menu-position = 2;
        clock-menu-position-offset = 0;
        panel-button-padding-size = 4;
        panel-indicator-padding-size = 4;
        workspace-background-corner-size = 4;
      };

      "org/gnome/shell/extensions/user-theme" = {
        name = "Ducktape Shell";
      };

      "org/gnome/terminal/legacy" = {
        default-show-menubar = false;
      };
    };
  };

  programs.gnome-shell = lib.mkIf enableGui {
    enable = true;
    extensions = [
      # Provides StatusNotifier/AppIndicator support for Timekpr and other
      # tray-based applications on every GNOME host.
      { package = pkgs.gnomeExtensions.appindicator; }
      { package = pkgs.gnomeExtensions.panel-date-format; }
      { package = pkgs.gnomeExtensions.cronomix; }
      { package = popShellPackage; }
      { package = pkgs.gnomeExtensions.gsconnect; }
      { package = pkgsUnstable.gnomeExtensions.just-perfection; }
      { package = pkgs.gnomeExtensions.user-themes; }
    ]
    # display-scale-switcher was removed from nixpkgs after 25.11 (absent in
    # 26.05 and unstable). Keep it only where it still exists (25.11 hosts); it
    # drops out on 26.05. TODO(nixpkgs-bump): revisit at the wholesale bump.
    ++ lib.optional (pkgs.gnomeExtensions ? display-scale-switcher) {
      package = pkgs.gnomeExtensions.display-scale-switcher;
    };
  };

  home.file.".cargo/config.toml".source = toTOML "cargo-config.toml" { };

  home.file.".ansible.cfg".source = (pkgs.formats.ini { }).generate "ansible.cfg" {
    defaults.collections_path = "~/.ansible/collections";
  };

  home.activation.warnLegacyNpmGlobal = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    [[ -d "$HOME/.npm-global" ]] && echo "⚠️  WARNING: Remove legacy ~/.npm-global directory (replaced by pnpm)"
  '';
}
