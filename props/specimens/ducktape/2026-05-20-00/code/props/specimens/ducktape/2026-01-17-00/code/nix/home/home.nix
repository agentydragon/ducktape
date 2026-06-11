{
  config,
  pkgs,
  lib,
  enableGui,
  enableKube,
  isNixOS,
  enableHeavyPackages,
  nix-colors,
  solarizedLight,
  solarizedDark,
  terminalFont,
  ...
}:
# IMPORTANT: Nix/Ansible Split for agentydragon machine
# =====================================================
# Nix home-manager manages:
#   - User-level packages (dev tools, language servers, formatters)
#   - GNOME dconf settings and terminal profiles
#   - XDG autostart entries
#   - GNOME extensions packages
#   - oh-my-zsh
#
# Ansible continues to manage:
#   - System packages (via apt)
#   - Some dotfiles via rcm
#   - Services and system configuration
#   - Build dependencies (libssl-dev, etc.)
#
# Tools where Nix takes precedence (moved to cli_nix_migrated in Ansible):
#   - neovim (Nix: unstable version)
#   - Node.js (Nix: nodejs_22)
#   - Rust (Nix: rustc/cargo packages)
# Note for NixOS systems with enableHeavyPackages:
# Heavy packages (gimp, krita, freecad, inkscape, etc.) should be installed
# via NixOS system configuration using the module at nix/nixos/heavy-packages-module.nix
# See heavy-packages.nix for the complete list.
let
  # Import the single source of truth for heavy packages
  heavyPkgs = import ./heavy-packages.nix;

  # Install heavy packages via home-manager only if:
  # 1. Heavy packages are enabled for this host
  # 2. This is NOT a NixOS system (NixOS uses system packages)
  installHeavyViaHomeManager = enableHeavyPackages && !isNixOS;

  gnomeNvim = pkgs.vimUtils.buildVimPlugin {
    pname = "gnome.nvim";
    version = "2024-11-26";
    src = pkgs.fetchFromGitHub {
      owner = "willmcpherson2";
      repo = "gnome.nvim";
      rev = "87e850c1e9422310ede4b70df90a6a89c16bb9e1";
      sha256 = "1zxq484k3mcppy21xiflmnji7j2n5zyc74ffbybhc9xasrgwa1nk";
    };
  };

  vimLumen = pkgs.vimUtils.buildVimPlugin {
    pname = "vim-lumen";
    version = "2024-11-26";
    src = pkgs.fetchFromGitHub {
      owner = "vimpostor";
      repo = "vim-lumen";
      rev = "97157aac9f0d24c144a3defdfe5057ee61e18dcb";
      sha256 = "1a32szs5hz9l1b1s1cfzbjvrn9wzqjkhffq9kaabvbpvlzd2hms9";
    };
  };

  # Helm/Helmfile wrapped with plugins (helm-diff)
  myKubernetesHelm = pkgs.wrapHelm pkgs.kubernetes-helm {
    plugins = with pkgs.kubernetes-helmPlugins; [
      helm-diff
    ];
  };

  myHelmfile = pkgs.helmfile-wrapped.override {
    inherit (myKubernetesHelm.passthru) pluginsDir;
  };

  # Shell initialization scripts (loaded from external files to avoid escaping hell)
  commonShellInit = builtins.readFile ./shell/common-init.sh;
  bashInit = builtins.readFile ./shell/bash-init.sh;
  zshInit = builtins.readFile ./shell/zsh-init.zsh;

  # ducktape - CLI tools collection (git-commit-ai, difftree)
  ducktape = pkgs.callPackage ./packages/ducktape.nix {};

  # headscale-cleanup - Headscale node management tool
  headscale-cleanup = pkgs.callPackage ./packages/headscale-cleanup.nix {};
in {
  imports = [
    # TODO: Re-enable google-drive-service once the git repo is accessible
    # Disabled during 25.11 migration due to 504 error from https://git.k3s.agentydragon.com/agentydragon/google-drive
    # ./packages/google-drive-service.nix
    ./codex
    ./crush
    ./modules/solarized.nix
    ./scripts
    ./terminals
    ./claude-code
    ./modules/gnome-workspace-shortcuts.nix
    ./modules/flameshot-screenshots.nix
    ./modules/datetime-format.nix
    ./services/login-event-webhook-reporter.nix
    ./services/activitywatch.nix
  ];
  nixpkgs.config.allowUnfree = true;
  # Home Manager needs a bit of information about you and the paths it should manage.
  home.username = "agentydragon";
  home.homeDirectory = "/home/agentydragon";

  # Home Manager release your configuration is compatible with.
  # NOTE: stateVersion is set per-host in hosts/*.nix files

  # Let Home Manager install and manage itself.
  programs.home-manager.enable = true;

  # XDG user directories - minimal setup, most point to $HOME
  xdg.userDirs = {
    enable = true;
    createDirectories = false; # Don't create directories, just set the config
    desktop = "$HOME";
    documents = "$HOME";
    download = "$HOME/downloads";
    music = "$HOME";
    pictures = "$HOME";
    publicShare = "$HOME";
    templates = "$HOME";
    videos = "$HOME";
  };

  # Google Drive service - disabled by default, enabled per-host
  # TODO: Re-enable when google-drive-service module is re-enabled (see imports above)
  # services.google-drive.enable = lib.mkDefault false;

  nix.package = pkgs.nix;

  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    download-buffer-size = 268435456; # 256MB (increased from default 64MB)

    # Add nix-community cache for home-manager, nixGL, etc.
    substituters = [
      "https://cache.nixos.org/"
      "https://nix-community.cachix.org"
    ];
    trusted-public-keys = [
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      "nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs="
    ];
  };

  programs.git = {
    enable = true;
    package = pkgs.git.override {withLibsecret = true;};
    lfs.enable = true;

    # Global gitignore file (migrated from dotfiles/config/git/ignore)
    ignores = [
      ".aider*"
      "__pycache__"
      "*.sw[op]"
      "**/.claude/settings.local.json"
      "**/CLAUDE.local.md"
      "oneoff__*" # Temporary one-off scripts
    ];

    settings = {
      user = {
        name = "Rai";
        email = "agentydragon@gmail.com";
      };
      core.autocrlf = false;
      color.ui = "auto";
      push.default = "upstream";
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
      rerere.enabled = true;
      init.defaultBranch = "main";
      merge.tool = "vimdiff";
      # Use libsecret credential helper for secure HTTPS token storage
      credential.helper = "libsecret";
      "url \"git@github.com:\"" = {
        insteadOf = [
          "https://github.com"
          "https://github.com/"
        ];
      };
      # nbdime difftool configuration
      "difftool \"nbdime\"".cmd = "git-nbdifftool diff \"$LOCAL\" \"$REMOTE\" \"$BASE\"";
      difftool.prompt = false;
      "mergetool \"nbdime\"".cmd = "git-nbmergetool merge \"$BASE\" \"$LOCAL\" \"$REMOTE\" \"$MERGED\"";
      mergetool.prompt = false;
    };
  };
  programs.neovim = {
    enable = true;
    viAlias = true;
    vimAlias = true;
    withNodeJs = false;
    withPython3 = false;
    extraLuaConfig = builtins.readFile ./config/nvim/init.lua;
  };

  # Delta - better git diffs
  programs.delta = {
    enable = true;
    enableGitIntegration = true; # Explicitly enable as suggested by warning
    options = {
      navigate = true;
      light = false; # Default to dark theme
      side-by-side = true;
      line-numbers = true;
      syntax-theme = "Solarized (dark)"; # Use same theme as bat
      features = "decorations";
      decorations = {
        commit-decoration-style = "bold yellow box ul";
        file-style = "bold yellow ul";
        file-decoration-style = "none";
        hunk-header-decoration-style = "cyan box ul";
      };
      line-numbers-left-style = "cyan";
      line-numbers-right-style = "cyan";
      line-numbers-minus-style = "124";
      line-numbers-plus-style = "28";
    };
  };

  # GPG configuration
  programs.gpg = {
    enable = true;
    settings = {
      # Use agent for key management
      use-agent = true;
      # Default key preferences (modern crypto)
      default-preference-list = "SHA512 SHA384 SHA256 AES256 AES192 AES ZLIB BZIP2 ZIP Uncompressed";
      personal-cipher-preferences = "AES256 AES192 AES";
      personal-digest-preferences = "SHA512 SHA384 SHA256";
      # UI preferences
      fixed-list-mode = true;
      keyid-format = "0xlong";
      with-fingerprint = true;
    };
  };

  # GPG Agent configuration
  services.gpg-agent = {
    enable = true;
    defaultCacheTtl = 28800; # 8 hours
    maxCacheTtl = 86400; # 24 hours
    pinentry.package = pkgs.pinentry-gtk2; # GUI pinentry for GNOME
  };

  # SSH Agent - holds decrypted SSH keys in memory
  services.ssh-agent.enable = true;

  # Readline configuration (migrated from dotfiles/inputrc)
  programs.readline = {
    enable = true;
    variables = {
      # Show all completion matches immediately on first tab (instead of requiring second tab)
      show-all-if-ambiguous = true;
    };
  };

  # Dircolors configuration (migrated from dotfiles/dir_colors/dircolors)
  programs.dircolors.enable = true;

  # AppImageLauncher configuration (migrated from dotfiles/config/appimagelauncher.cfg)
  xdg.configFile."appimagelauncher.cfg".text = ''
    [AppImageLauncher]
    %23%20%23%20additional_directories_to_watch=~/otherApplications:/even/more/applications
    %23%20%23%20monitor_mounted_filesystems=false
    ask_to_move=true
    destination=/home/agentydragon/.local/appimages
    enable_daemon=true
  '';

  # Neovim configuration (sync entire dotfiles directory)
  xdg.configFile."nvim" = {
    source = ./config/nvim;
    recursive = true;
  };
  # Base bazelrc settings (layered by popos-bazel.nix and host configs)
  home.file.".bazelrc".text = ''
    common --show_progress_rate_limit=0.05
    common --progress_in_terminal_title
    common --enable_bzlmod
    build --platforms //:linux_x64
  '';

  # Packages to install (Phase 1: only actual user-level packages from Ansible)
  home.packages = with pkgs;
    [
      # Python development environment
      (python3.withPackages (ps:
        with ps; [
          autopep8
          pydeps
          black
          isort
          pandas
          torch
          numpy
        ]))

      pkgs.pyright

      ansible
      ast-grep
      awscli2
      bazelisk
      gnuplot
      jq
      mc
      mmv
      nethogs
      pre-commit
      ruff
      speedtest-cli
      terraform
      uv
      xxd
      yq
      zsh
      atuin

      # Tools from GitHub releases / binary downloads
      gh
      glab
      gitstatus

      # Node/JS dev
      nodejs_24
      nodePackages.pnpm
      bun

      # Rust toolchain - all from Nix to ensure consistent glibc
      # This allows removing CC=/usr/bin/gcc from .envrc since Nix gcc matches Nix glibc
      rustc
      cargo
      clippy
      rustfmt
      rust-analyzer
      sccache
      gcc # C compiler from Nix - matches Nix glibc for native extension builds
      # jscpd and madge are not in nixpkgs - install manually with: pnpm add -g jscpd madge

      # Development languages/compilers
      go
      # python312 moved to python3.withPackages in solarized.nix to avoid collision

      # Development tools
      direnv
      devenv
      alejandra # Nix formatter
      rclone # Cloud storage mounting/sync
      opencode # AI coding agent for terminal

      # Tree-sitter CLI for manual parser management
      tree-sitter # Used by nvim-treesitter auto_install

      # Formatters for conform.nvim
      stylua # Lua formatter

      # Custom packages from ducktape repo
      ducktape # CLI tools: git-commit-ai, difftree
      headscale-cleanup # Headscale node management
    ]
    ++ lib.optionals enableKube [
      kubectl
      myKubernetesHelm
      kubeseal
      myHelmfile
    ]
    ++ [
      # Dotfile management (keeping rcm approach)
      rcm

      # Modern ls replacement with colors and icons
      eza

      # Smarter cd command that learns your habits
      zoxide

      # Command-line fuzzy finder
      fzf
      # Find alternative with sensible defaults
      fd
      # Fast recursive search to pair with fd and fzf
      ripgrep
      # Rich TUI resource monitors for system overview
      btop
      bottom
      # Modern process viewer with structured output
      procs
      # Disk usage visualizer with intuitive tree view
      dust
      # Source lines of code analyzer grouped by language
      tokei
      # Network diagnostics (per-process usage and path tracing)
      bandwhich
      mtr

      curl
      wget
      pwgen
      nmap
      htop
      iftop
      iotop
      ffmpeg
      mosh
      ncdu
      pv
      tree
      sqlite
      gnupg

      # Prompt themes (switchable via USE_OHMYPOSH env var)
      oh-my-posh # Cross-shell prompt with proper powerline support
      zsh-powerlevel10k # Powerlevel10k theme for zsh

      # vertical-workspaces managed by gnome-workspace-shortcuts module
    ]
    ++ lib.optionals enableGui [
      # Fonts - using modern individual nerd-fonts packages (covers ansible nerd_fonts role)
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

      # Additional fonts
      roboto

      # GNOME Shell Extensions (migrated from Ansible role petermosmans.customize-gnome):
      # gnomeExtensions.desaturated-tray-icons  # ID 1102: Not currently used
      gnomeExtensions.panel-date-format # ID 1462: Panel Date Format ✓
      # night-theme-switcher managed by solarized module
      gnomeExtensions.vertical-workspaces # ID 5177: V-Shell (Vertical Workspaces) ✓
      gnomeExtensions.cronomix # ID 6003: Cronomix ✓
      # Note: Pop!_OS includes ubuntu-appindicators, so gnomeExtensions.appindicator not needed
    ]
    ++ lib.optionals enableGui [
      # GUI applications (migrated from Ansible)
      # Note: discord and element-desktop moved to heavy packages

      # Development & utilities
      flameshot
      xclip # X11 clipboard utility

      # Media players (lightweight alternatives)
      mplayer
      mpv

      # Image viewer
      geeqie

      # System utilities
      scrcpy # Android screen mirroring
      virt-viewer # SPICE/VNC viewer for virtual machines (Proxmox viewer)

      # GNOME utilities
      gnome-tweaks
      dconf-editor
    ]
    ++ lib.optionals installHeavyViaHomeManager (heavyPkgs.heavyPackages pkgs)
    ++ [
      # CLI utilities (no GUI needed)
      yt-dlp # YouTube downloader
      pdftk # PDF manipulation toolkit
      qpdf # PDF transformation/inspection tool

      # TODO: comby is marked as broken in nixpkgs 25.11
      # Previously we got it from oldPkgs (nixos-23.11) but removed during 25.11 migration
      # Options: 1) build from source, 2) use unstable pin if fixed there, 3) find alternative
      # pkgs.comby
    ];

  # Enable fontconfig for proper font management (only when GUI is enabled)
  fonts.fontconfig.enable = enableGui;

  # Session variables (migrated from dotfiles/profile)
  home.sessionVariables = {
    # Editor
    EDITOR = "nvim";
    VISUAL = "nvim";

    # Basic Memory location
    BASIC_MEMORY_HOME = "$HOME/.syncthing/pkm/basic-memory";

    # Character encoding
    DEFAULT_CHARSET = "utf8";

    # Aider AI model
    AIDER_MODEL = "o1";

    # GCC colored warnings and errors
    GCC_COLORS = "error=01;31:warning=01;35:note=01;36:caret=01;32:locus=01:quote=01";

    # Interactive shell settings
    LESS = "-F -X -R"; # -F: exit if one screen, -X: no clear screen, -R: raw ANSI colors
    PYTHONSTARTUP = "$HOME/.config/pythonstartup.py";

    # Go workspace
    GOPATH = "$HOME/.go";

    # pnpm global packages
    PNPM_HOME = "$HOME/.local/share/pnpm";
  };

  # XDG MIME type associations - SKIPPED
  # We need to ensure these 2 specific associations because they tend to get
  # incorrectly assigned, BUT the existing mimeapps.list has 105 lines of
  # associations we want to preserve. Home-manager can't merge, only replace.
  # TODO: Either:
  #   - Keep in Ansible (which can do in-place edits)
  #   - Write activation script to patch these 2 entries
  #   - Import all 105 associations into Nix (tedious but complete)
  # For now, keeping in Ansible.
  # xdg.mimeApps = {
  #   enable = true;
  #   defaultApplications = {
  #     "text/html" = ["google-chrome.desktop"];  # Often gets set to wrong browser
  #     "application/x-virt-viewer" = ["virt-viewer.desktop"];  # Gets set incorrectly
  #   };
  # };

  # XDG autostart desktop entries (migrated from Ansible gui role)
  xdg.configFile."autostart/syncthing-gtk.desktop".text = ''
    [Desktop Entry]
    Type=Application
    Name=Syncthing-GTK
    Exec=syncthing-gtk --minimized
    Icon=syncthing-gtk
    Terminal=false
    Categories=Network;FileTransfer;
    X-GNOME-Autostart-enabled=true
  '';
  xdg.configFile."autostart/discord.desktop".text = ''
    [Desktop Entry]
    Type=Application
    Name=Discord (Minimized)
    Exec=discord --start-minimized
    Icon=discord
    Terminal=false
    Categories=Network;InstantMessaging;
    X-GNOME-Autostart-enabled=true
  '';

  # GNOME dconf settings (migrated from Ansible gui role)
  dconf = {
    enable = true;
    settings = {
      # GNOME preferences
      "org/gnome/desktop/wm/preferences" = {
        focus-mode = "sloppy"; # Focus follows mouse
        button-layout = ":minimize,maximize,close"; # Window buttons
      };

      # Terminal shortcut (Ctrl+Alt+T)
      "org/gnome/settings-daemon/plugins/media-keys" = {terminal = ["<Primary><Alt>t"];};

      # GNOME Night Light
      "org/gnome/settings-daemon/plugins/color" = {
        night-light-enabled = true;
        night-light-temperature = lib.hm.gvariant.mkUint32 2414;
      };

      # ISO 8601 datetime format in panel, e.g.: "Wed 2023-11-15 22:49"
      "org/gnome/shell/extensions/panel-date-format" = {format = "%a %Y-%m-%d %H:%M";};

      # Legacy datetime indicator (for older WMs/Unity?)
      "com/canonical/indicator/datetime" = {
        time-format = "custom";
        custom-time-format = "%Y-%m-%d %H:%M:%S";
        show-week-numbers = true;
      };

      "org/gnome/terminal/legacy" = {default-show-menubar = false;};

      "org/gnome/shell" = {
        # Enable user extensions
        disable-user-extensions = false;

        # IMPORTANT: This REPLACES the entire enabled-extensions list, not appends!
        # This list is a union of:
        # 1. Current system extensions (Pop!_OS defaults)
        # 2. Extensions from Ansible configuration
        # 3. Minus the problematic ones we disable below
        enabled-extensions = [
          # Pop!_OS system extensions (keep these!)
          "ding@rastersoft.com" # Desktop Icons NG (DING)
          "pop-cosmic@system76.com" # Pop COSMIC
          "pop-shell@system76.com" # Pop Shell (tiling)
          "system76-power@system76.com" # System76 Power
          "ubuntu-appindicators@ubuntu.com" # Ubuntu AppIndicators (system tray)
          "cosmic-dock@system76.com" # COSMIC Dock
          # Note: cosmic-workspaces and popx11gestures excluded (problematic)

          # Extensions from Ansible (petermosmans.customize-gnome)
          "panel-date-format@keiii.github.com" # Panel Date Format
          # nightthemeswitcher managed by solarized module
          "vertical-workspaces@G-dH.github.com" # V-Shell (replaces cosmic-workspaces)
          "cronomix@zagortenay333" # Cronomix (note: different UUID than expected)
          # Note: Desaturate All extension not currently installed
        ];

        # Disable problematic Pop!_OS extensions
        disabled-extensions = [
          "cosmic-workspaces@system76.com"
          "popx11gestures@system76.com"
        ];
      };
    };
  };

  # Common shell configuration
  home.shellAliases = {
    ".." = "cd ..";
    suspend = "systemctl suspend";
    npm = "pnpm";
    bazel = "bazelisk"; # Use bazelisk to auto-download correct Bazel version per .bazelversion
    npx = "echo '❌ No you idiot, use pnpm dlx' && false";
    gmrc = "glab mr create --fill --remove-source-branch --yes";
    vimdiff = "nvim -d";
    alert = ''notify-send --urgency=low -i "$([ $? = 0 ] && echo terminal || echo error)" "$(history|tail -n1|sed -e 's/^\s*[0-9]\+\s*//;s/[;&|]\s*alert$//')"'';

    # Custom eza aliases (beyond what programs.eza provides)
    lt = "eza -l --tree --icons=auto --group-directories-first";
    lS = "eza -l --sort=size --reverse --icons=auto --group-directories-first";
    ld = "eza -l --only-dirs --icons=auto --group-directories-first";
    l1 = "eza -1 --icons=auto";
    lm = "eza -l --sort=modified --reverse --icons=auto --group-directories-first";
  };

  # GNOME Terminal profiles handled by solarized module

  # Zsh configuration - full Nix management
  programs.zsh = {
    enable = true;

    # .zshenv content (loaded for all zsh invocations, including scripts)
    envExtra = "skip_global_compinit=1";

    # No auto-correction
    enableCompletion = true;
    autocd = true;

    autosuggestion = {
      enable = true;
      strategy = ["history" "completion"];
      highlight = "fg=244";
    };

    syntaxHighlighting.enable = true;

    oh-my-zsh = {
      enable = true;
      plugins = [
        "alias-finder"
        "bazel"
        "aliases"
        "colored-man-pages"
        "command-not-found"
        "docker"
        "git"
        "gpg-agent"
        "isodate"
        "lein"
        "python"
        "rust"
      ];
    };

    # p10k plugin loaded conditionally in zsh-init.zsh based on USE_OHMYPOSH env var
    plugins = [
      {
        name = "powerlevel10k";
        src = pkgs.zsh-powerlevel10k;
        file = "share/zsh-powerlevel10k/powerlevel10k.zsh-theme";
      }
    ];

    sessionVariables = {
      ZSH_ALIAS_FINDER_AUTOMATIC = "true";
      COMPLETION_WAITING_DOTS = "%F{yellow}...%f";
      DISABLE_UNTRACKED_FILES_DIRTY = "true";
      RPROMPT = "%*";
      DEFAULT_USER = "agentydragon";
      ZSH_THEME_TERM_TITLE_IDLE = "%n: %~ $";
    };

    # Additional initialization (loaded after oh-my-zsh)
    initContent = lib.mkMerge [
      (zshInit + "\n" + commonShellInit)

      # Conditional zoxide integration for Claude Code compatibility (after everything else)
      # Only initialize zoxide when NOT running in Claude Code to prevent function
      # definition conflicts. Claude Code filters out functions starting with '_' or '__',
      # breaking zoxide's __zoxide_z() function which cd() depends on.
      # See: ../docs/claude-code-shell.md for details
      (lib.mkOrder 1400 ''
        if [[ -z "$CLAUDECODE" ]]; then
          eval "$(${lib.getExe pkgs.zoxide} init zsh --cmd cd)"
        fi
      '')
    ];
  };

  # Bash configuration - full Nix management
  programs.bash = {
    enable = true;
    enableCompletion = true;

    shellOptions = [
      "checkwinsize"
      "globstar"
    ];

    # Bash-specific initialization
    initExtra = bashInit + "\n" + commonShellInit;
  };

  # Atuin - better shell history
  programs.atuin = {
    enable = true;
    enableBashIntegration = true;
    enableZshIntegration = true;
    flags = ["--disable-up-arrow"];
    # zsh and bash have no fancy history config, Atuin handles it
  };

  # Direnv - per-directory environment management
  programs.direnv = {
    enable = true;
    enableBashIntegration = true;
    enableZshIntegration = true;
    nix-direnv.enable = true;
  };

  # Zoxide - smarter cd (conditionally disabled for Claude Code)
  programs.zoxide = {
    enable = true;
    enableBashIntegration = false; # Disabled for bash - disorients Claude/Codex assistants
    enableZshIntegration = false; # Disabled - using custom conditional integration below
    options = ["--cmd cd"];
  };

  # Eza - modern ls replacement
  programs.eza = {
    enable = true;
    enableBashIntegration = true;
    enableZshIntegration = true;
    icons = "auto";
    git = true;
    extraOptions = [
      "--group-directories-first"
      "--header"
    ];
  };

  # Tmux configuration with plugins (migrated from dotfiles/tmux.conf)
  programs.tmux = {
    enable = true;
    sensibleOnTop = true;

    # Basic settings
    mouse = true;
    historyLimit = 100000;
    baseIndex = 1; # Start windows at 1
    keyMode = "vi"; # Vi mode keys
    clock24 = true;
    prefix = "C-b";
    # terminal = "tmux-256color";  # Better terminal type for modern tmux

    # Plugins from TPM configuration
    plugins = with pkgs.tmuxPlugins; [
      resurrect # Save/restore sessions
      continuum # Auto-save sessions periodically
      yank # System clipboard integration
      prefix-highlight # Show prefix/copy/sync modes in status
    ];

    # Main tmux configuration (migrated from tmux.conf)
    extraConfig = ''
      # Pane border titles - show pane title or current command
      set -g pane-border-status top
      set -g pane-border-format ' #{?pane_title,#{pane_title},#{pane_current_command}} '

      # Window/Pane titles
      set -g set-titles on
      set -g set-titles-string '#S:#I.#P #W'
      set -g allow-rename on
      set -g automatic-rename on

      # Status bar update interval
      set -g status-interval 2

      # Start panes at 1 (like windows)
      setw -g pane-base-index 1

      # Enable vi mode in copy mode
      setw -g mode-keys vi

      # Split bindings (| for horizontal, - for vertical)
      bind | split-window -h
      bind - split-window -v
      unbind '"'
      unbind %

      # Pane navigation with vim keys (h/j/k/l) - repeatable with prefix
      unbind -n C-h
      unbind -n C-j
      unbind -n C-k
      unbind -n C-l
      set -g repeat-time 400
      bind -T prefix -r h select-pane -L
      bind -T prefix -r j select-pane -D
      bind -T prefix -r k select-pane -U
      bind -T prefix -r l select-pane -R

      # Resize panes with Alt + arrows
      bind -n M-Left  resize-pane -L 5
      bind -n M-Right resize-pane -R 5
      # M-Up reserved for Codex CLI (queued prompt retrieval), so leave it unbound here.
      unbind -n M-Up
      bind -n M-Down  resize-pane -D 2

      # Clipboard integration
      set -g set-clipboard on

      # Copy mode (vi) key bindings (tmux-yank handles clipboard integration via xclip)
      bind -T copy-mode-vi v send -X begin-selection
      bind -T copy-mode-vi y send -X copy-selection-and-cancel
      bind -T copy-mode-vi Y send -X copy-line

      # Status bar configuration
      set -g status-left-length 60
      set -g status-right-length 60
      set -g status-left "#S #[fg=cyan]| #[default]#I:#W"
      set -g status-right "#{prefix_highlight} #(whoami) #[fg=cyan]| %Y-%m-%d %H:%M"

      # Plugin settings
      # prefix-highlight configuration
      set -g @prefix_highlight_show_copy_mode on
      set -g @prefix_highlight_show_sync_mode on

      # Ensure tmux refreshes SSH-related env vars when reattaching locally, so p10k context
      # doesn't think we're still in an old SSH session.
      set -g update-environment "DISPLAY SSH_ASKPASS SSH_AUTH_SOCK SSH_AGENT_PID SSH_CONNECTION"

      # tmux-resurrect settings
      set -g @resurrect-strategy-nvim 'session'
      set -g @resurrect-strategy-vim 'session'

      # tmux-continuum settings
      set -g @continuum-restore 'on'

      # Force proper terminal and enable true color support
      set -g default-terminal "tmux-256color"
      set -ag terminal-overrides ",xterm-256color:RGB"

      # Enable hyperlink support (OSC 8) for clickable links in terminal
      set -as terminal-features ',*:hyperlinks'
    '';
  };

  # Prompt configurations (switchable via USE_OHMYPOSH env var)
  # TODO: oh-my-posh being tested, not working currently
  xdg.configFile."oh-my-posh/config.json".source = ./ohmyposh.json;
  home.file.".p10k.zsh".source = ./p10k.zsh;

  # Create Worthy config directory
  home.file.".config/worthy/.keep".text = "";

  # Cargo configuration - use sccache for compilation caching
  home.file.".cargo/config.toml".text = ''
    [build]
    rustc-wrapper = "sccache"
  '';

  # Ansible configuration
  home.file.".ansible.cfg".text = ''
    [defaults]
    collections_path = ~/.ansible/collections
  '';

  # Warn if legacy .npm-global directory exists (should be removed in favor of pnpm)
  home.activation.warnLegacyNpmGlobal = lib.hm.dag.entryAfter ["writeBoundary"] ''
    [[ -d "$HOME/.npm-global" ]] && echo "⚠️  WARNING: Remove legacy ~/.npm-global directory (replaced by pnpm)"
  '';

  # Additional Claude Code MCP wiring is handled via programs.claude-code.
}
