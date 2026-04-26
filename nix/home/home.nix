{
  config,
  pkgs,
  pkgsUnstable,
  lib,
  enableGui,

  isNixOS,
  isK8sWorker,
  nix-colors,
  solarizedLight,
  solarizedDark,
  terminalFont,
  ducktape-artifacts,
  gaffer-private,
  ...
}:
let
  toTOML = (pkgs.formats.toml { }).generate;

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

  solarizedNvim = pkgs.vimUtils.buildVimPlugin {
    pname = "solarized.nvim";
    version = "2024-11-26";
    src = pkgs.fetchFromGitHub {
      owner = "maxmx03";
      repo = "solarized.nvim";
      rev = "main";
      sha256 = "1fz1wc569w26aanmj3hhsc17xrx29g6bfsjsbgssa7jq76aavp3w";
    };
  };

  # Shell initialization scripts (loaded from external files to avoid escaping hell)
  commonShellInit = builtins.readFile ./shell/common-init.sh;
  bashInit = builtins.readFile ./shell/bash-init.sh;
  zshInit = builtins.readFile ./shell/zsh-init.zsh;

  ducktapePackages = import ../packages {
    inherit lib pkgs;
    artifacts = ducktape-artifacts;
  };
  inherit (ducktapePackages)
    ducktape
    claude-hooks
    gterm-theme
    bbapi
    ;
in
{
  # Expose the full package set so host configs can use per-host packages
  # (e.g., tana, bebas-neue-font) without re-importing nix/packages/.
  _module.args.ducktapePackages = ducktapePackages;

  imports = [

    # google-drive module imported via mkHome extraModules (avoids _module.args cycle)
    ./codex
    ./crush
    ./modules/solarized.nix
    ./terminals
    ./claude_code
    ./programs/gemini-cli.nix # Our local module with policies support
    ./gemini_cli.nix # Configuration using the local module
    ./shell/oh-my-posh.nix
    ./modules/gnome-shell-keybindings.nix
    ./modules/gnome-custom-keybindings.nix
    ./modules/flameshot-screenshots.nix
    ./modules/attic.nix
    ./modules/buildbuddy.nix
    ./modules/datetime-format.nix
    ./modules/sops-env.nix
    ./services/activitywatch.nix
    ./opencode
  ];
  ducktape.sopsEnv = {
    HF_TOKEN = {
      sopsFile = ../../secrets/shared/huggingface.yaml;
      key = "hf_token";
    };
    HABITIFY_API_KEY = {
      sopsFile = ../../secrets/shared/habitify.yaml;
      key = "habitify_api_key";
    };
  };

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

  services.google-drive.enable = lib.mkDefault false;

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
    ];
    download-buffer-size = 268435456; # 256MB (increased from default 64MB)
    connect-timeout = 5; # Fail fast on unreachable substituters

    # Add nix-community cache for home-manager, nixGL, etc.
    # Add self-hosted attic binary cache for CI-built closures.
    # CLEANUP(2026-04-02): Re-enable cache.allegedly.works when cluster is back up
    substituters = [
      # "https://cache.allegedly.works/main"
      "https://cache.nixos.org/"
      # "https://nix-community.cachix.org"
    ];
    trusted-public-keys = [
      "cache.allegedly.works-1:OX/cis8G1W13DALkGvhdUZ1OY3yGATbXw8+tIc8J7oA="
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
      # "nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs="
    ];
  };

  programs.git = {
    enable = true;
    # CLEANUP(2026-04-12): Remove doCheck override once git 2.51.2 test failures
    #   are fixed in nixpkgs (t1092-sparse-checkout-compatibility.sh).
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
      # Disable fork-point detection during pull --rebase. Fork-point uses
      # the remote-tracking reflog to guess which local commits are "already
      # upstream" after a force-push, and silently drops them. This caused
      # a local commit to be lost after push + immediate pull when the remote
      # was force-updated between the two operations.
      rebase.forkPoint = false;
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
    plugins = with pkgs.vimPlugins; [
      (nvim-treesitter.withPlugins (
        p: with p; [
          bash
          bibtex
          c
          c-sharp
          clojure
          cmake
          cpp
          css
          csv
          desktop
          diff
          dockerfile
          git-config
          git-rebase
          gitattributes
          gitcommit
          gitignore
          go
          gomod
          gosum
          gotmpl
          haskell
          html
          htmldjango
          http
          ini
          java
          javadoc
          javascript
          jinja
          jq
          jsdoc
          json
          jsonnet
          latex
          lua
          luadoc
          make
          markdown
          nginx
          nix
          proto
          python
          requirements
          rust
          scss
          sql
          ssh-config
          starlark
          textproto
          tmux
          toml
          typescript
          vim
          vimdoc
          xml
        ]
      ))
      {
        plugin = nvim-lspconfig;
        type = "lua";
        config = ''
          vim.lsp.config("pyright", {})
          vim.lsp.enable("pyright")
        '';
      }
      {
        plugin = conform-nvim;
        type = "lua";
        config = ''
          require("conform").setup({
            formatters_by_ft = {
              lua = { "stylua" },
              python = { "isort", "black" },
              rust = { "rustfmt", lsp_format = "fallback" },
            },
            format_on_save = {
              timeout_ms = 500,
              lsp_format = "fallback",
            },
          })
        '';
      }
      {
        plugin = copilot-lua;
        type = "lua";
        config = ''
          require("copilot").setup({
            suggestion = { enabled = true, auto_trigger = true },
            panel = { enabled = false },
            filetypes = {
              markdown = true,
              help = true,
              gitcommit = true,
              ["*"] = true,
            },
          })
        '';
      }
      nvim-web-devicons
      {
        plugin = lualine-nvim;
        type = "lua";
        config = ''
          require("lualine").setup({
            options = { icons_enabled = true, theme = "auto" },
          })
        '';
      }
      {
        plugin = nvim-notify;
        type = "lua";
        config = ''
          local bg_color = vim.o.background == "dark" and "#002b36" or "#fdf6e3"
          require("notify").setup({ background_colour = bg_color })
          vim.notify = require("notify")
        '';
      }
      {
        plugin = vim-better-whitespace;
        type = "lua";
        config = ''
          vim.g.better_whitespace_enabled = 1
          vim.api.nvim_set_hl(0, "ExtraIndentMixed", { bg = "#443333" })
          vim.api.nvim_create_autocmd("BufWinEnter", {
            callback = function()
              vim.fn.matchadd("ExtraIndentMixed", [[^\t+ +\|^ \+\t+]])
            end,
          })
          vim.api.nvim_set_hl(0, "ExtraWhitespace", { bg = "#552222" })
        '';
      }
      vim-lastplace
      {
        plugin = solarizedNvim;
        type = "lua";
        config = ''
          vim.o.termguicolors = true
          require("solarized").setup({})
          vim.cmd.colorscheme("solarized")
        '';
      }
      {
        plugin = gnomeNvim;
        type = "lua";
        config = ''
          if vim.fn.has("unix") == 1 and vim.fn.has("mac") == 0 then
            require("gnome").setup({})
          end
        '';
      }
      vimLumen
    ];
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

  # sops-nix: decrypt user secrets using ~/.ssh/id_ed25519 at home-manager activation time
  sops.age.sshKeyPaths = [ "${config.home.homeDirectory}/.ssh/id_ed25519" ];

  programs.ssh = {
    enable = true;
    enableDefaultConfig = false;
    matchBlocks = {
      homeassistant = {
        hostname = "10.0.0.3";
        user = "root";
        identityFile = "~/.ssh/15leroy";
        port = 22;
      };
    };
  };

  programs.readline = {
    enable = true;
    variables = {
      # Show all completion matches immediately on first tab (instead of requiring second tab)
      show-all-if-ambiguous = true;
    };
  };

  programs.dircolors.enable = true;

  xdg.configFile."appimagelauncher.cfg".text = ''
    [AppImageLauncher]
    %23%20%23%20additional_directories_to_watch=~/otherApplications:/even/more/applications
    %23%20%23%20monitor_mounted_filesystems=false
    ask_to_move=true
    destination=/home/agentydragon/.local/appimages
    enable_daemon=true
  '';

  # Neovim plugin config is now fully inline in programs.neovim.plugins above.
  # Base bazelrc settings (layered by host configs)
  home.file.".bazelrc".text = ''
    common --show_progress_rate_limit=0.05
    common --progress_in_terminal_title
    build --platforms //:linux_x64

    # Optional BuildBuddy / remote cache config (file not in git)
    try-import ${config.home.homeDirectory}/.config/bazel/buildbuddy.bazelrc
  '';

  # Packages to install
  home.packages =
    with pkgs;
    [
      # Python development environment
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

      # Tools from GitHub releases / binary downloads
      gh
      glab
      gitstatus

      # Node/JS dev
      nodejs_24
      nodePackages.pnpm
      bun

      # Rust toolchain - all from Nix to ensure consistent glibc
      # Allows removing CC=/usr/bin/gcc from .envrc since Nix gcc matches Nix glibc
      rustc
      cargo
      clippy
      rust-analyzer
      sccache
      gcc # Matches Nix glibc for native extension builds
      # jscpd, madge not in nixpkgs - install with: pnpm add -g jscpd madge

      # Development languages/compilers
      go
      # python312 moved to python3.withPackages in solarized.nix to avoid collision

      # Development tools
      direnv
      devenv
      rclone # Cloud storage mounting/sync
      pkgsUnstable.opencode # AI coding agent for terminal (unstable for faster updates)

      # Formatters for conform.nvim
      stylua # Lua formatter

      # Custom packages from ducktape repo
      ducktape # git-commit-ai, difftree, gmail-archiver
      claude-hooks # Claude Code hooks/statusline
      bbapi # BuildBuddy API CLI
      gterm-theme # GNOME Terminal theme follower
      ducktapePackages.tana # Knowledge graph / note-taking
    ]
    ++ [
      eza # Modern ls
      zoxide # Smarter cd
      fzf
      fd
      tokei # SLOC analyzer grouped by language

      pwgen
      ffmpeg
      sqlite
      gnupg

      # Prompt themes (switchable via USE_OHMYPOSH env var)
      oh-my-posh # Cross-shell prompt with proper powerline support
      zsh-powerlevel10k # Powerlevel10k theme for zsh

      yt-dlp # YouTube downloader
      pdftk # PDF manipulation toolkit
      qpdf # PDF transformation/inspection tool
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

      # GNOME Shell extensions are managed via programs.gnome-shell.extensions (see below).
      # Packages and enabled-extensions dconf are handled by that module.

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
      # virt-viewer is NOT installed via Nix — its spice-client-glib-usb-acl-helper
      # needs setuid root for USB redirection, which Nix can't provide outside NixOS.
      # Install via system package manager instead (apt on atlas — see ansible/atlas.yaml).

      # Learning
      anki

      # GNOME utilities
      gnome-tweaks
      dconf-editor

      # TODO: comby is marked as broken in nixpkgs 25.11
    ];

  # On non-NixOS (Proxmox, Pop!_OS, etc.), set up XDG_DATA_DIRS so GNOME Shell
  # can find nix-managed extensions, desktop files, and icons.
  targets.genericLinux.enable = !isNixOS;

  # Enable fontconfig when GUI is enabled
  fonts.fontconfig.enable = enableGui;

  home.sessionVariables = {
    EDITOR = "nvim";
    VISUAL = "nvim";

    # Character encoding
    DEFAULT_CHARSET = "utf8";

    # GCC colored warnings and errors
    GCC_COLORS = "error=01;31:warning=01;35:note=01;36:caret=01;32:locus=01:quote=01";

    # Interactive shell settings
    LESS = "-F -X -R"; # -F: exit if one screen, -X: no clear screen, -R: raw ANSI colors

    # Go workspace
    GOPATH = "$HOME/.go";

    # pnpm global packages
    PNPM_HOME = "$HOME/.local/share/pnpm";

  };

  # Patch 2 critical MIME associations in-place without replacing the full
  # mimeapps.list (desktop environment manages the rest).
  home.activation.fixMimeApps = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    run ${pkgs.xdg-utils}/bin/xdg-mime default google-chrome.desktop text/html
    run ${pkgs.xdg-utils}/bin/xdg-mime default remote-viewer.desktop application/x-virt-viewer
  '';

  # Terminal shortcut (Ctrl+Alt+T) — GNOME 49 removed the built-in 'terminal'
  # media key, so we use a custom keybinding via xdg-terminal-exec.
  ducktape.gnomeCustomKeybindings.terminal = {
    name = "Launch Terminal";
    command = "xdg-terminal-exec";
    binding = "<Primary><Alt>t";
  };

  # Default terminal for xdg-terminal-exec (used by Ctrl+Alt+T keybinding above)
  xdg.configFile."xdg-terminals.list".text = "org.gnome.Terminal.desktop\n";

  xdg.autostart = {
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

  # GNOME dconf settings (migrated from Ansible gui role)
  dconf = {
    enable = true;
    settings = {
      # GNOME preferences
      "org/gnome/desktop/wm/preferences" = {
        focus-mode = "sloppy"; # Focus follows mouse
        button-layout = ":minimize,maximize,close"; # Window buttons
      };

      # GNOME Night Light
      "org/gnome/settings-daemon/plugins/color" = {
        night-light-enabled = true;
        night-light-temperature = lib.hm.gvariant.mkUint32 2414;
      };

      # K8s workers should not auto-suspend on AC power
      "org/gnome/settings-daemon/plugins/power" = lib.mkIf isK8sWorker {
        sleep-inactive-ac-type = "nothing";
      };

      # ISO 8601 datetime format in panel, e.g.: "Wed 2023-11-15 22:49"
      "org/gnome/shell/extensions/panel-date-format" = {
        format = "%a %Y-%m-%d %H:%M";
      };

      "org/gnome/terminal/legacy" = {
        default-show-menubar = false;
      };

    };
  };

  # GNOME Shell extensions — managed via programs.gnome-shell module.
  # This sets dconf enabled-extensions and installs packages automatically.
  # Night-theme-switcher is added by solarized module.
  # Appindicator is added by NixOS host files (iguana, rugged, wyrm2).
  #
  # Other GNOME tiling options to consider:
  #   - gnomeExtensions.forge: tree-based auto-tiling (i3-style), good keybinding customization
  #   - gnomeExtensions.tiling-assistant: lighter touch, extends GNOME's built-in half/quarter snapping
  #   - gnomeExtensions.gtile: grid-based manual tiling (pick zones)
  #   - gnomeExtensions.tiling-shell: newer, customizable drag-and-drop zone layouts
  # Non-GNOME alternatives: hyprland (best NixOS integration), sway (i3 for Wayland), niri (scrollable tiling)
  programs.gnome-shell = lib.mkIf enableGui {
    enable = true;
    extensions = [
      { package = pkgs.gnomeExtensions.panel-date-format; }
      { package = pkgs.gnomeExtensions.cronomix; }
      { package = pkgs.gnomeExtensions.pop-shell; }
      # Phone integration (firewall ports opened by programs.kdeconnect in gui.nix).
      { package = pkgs.gnomeExtensions.gsconnect; }
      { package = pkgs.gnomeExtensions.display-scale-switcher; }
    ];
  };

  # Common shell configuration
  home.shellAliases = {
    ".." = "cd ..";
    suspend = "systemctl suspend";
    npm = "pnpm";
    npx = "echo '❌ No you idiot, use pnpm dlx' && false";
    gmrc = "glab mr create --fill --remove-source-branch --yes";
    gs = "git status --short --branch --show-stash";
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
    # TODO: Source nix-daemon.sh here for non-login shells (mosh). See nix/TODO.md.
    envExtra = "skip_global_compinit=1";

    # No auto-correction
    enableCompletion = true;
    autocd = true;

    autosuggestion = {
      enable = true;
      strategy = [
        "history"
        "completion"
      ];
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
      # Claude's shell snapshot filters functions starting with '_'/'__',
      # so __zoxide_z() is lost but cd() (which calls it) is kept → "command not found".
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
    flags = [ "--disable-up-arrow" ];
    settings = {
      sync_address = "https://atuin.allegedly.works";
    };
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
    options = [ "--cmd cd" ];
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
    terminal = "tmux-256color"; # Better terminal type for modern tmux

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

      # Enable true color support for xterm-256color terminals
      set -ag terminal-overrides ",xterm-256color:RGB"

      # Enable hyperlink support (OSC 8) for clickable links in terminal
      set -as terminal-features ',*:hyperlinks'
    '';
  };

  # Prompt configurations (switchable via USE_OHMYPOSH env var)
  # oh-my-posh config is in shell/oh-my-posh.nix
  home.file.".p10k.zsh".source = ./p10k.zsh;

  # Cargo configuration - use sccache for compilation caching
  home.file.".cargo/config.toml".source = toTOML "cargo-config.toml" {
    build.rustc-wrapper = "sccache";
  };

  # Ansible configuration
  home.file.".ansible.cfg".source = (pkgs.formats.ini { }).generate "ansible.cfg" {
    defaults.collections_path = "~/.ansible/collections";
  };

  # Warn if legacy .npm-global directory exists (should be removed in favor of pnpm)
  home.activation.warnLegacyNpmGlobal = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    [[ -d "$HOME/.npm-global" ]] && echo "⚠️  WARNING: Remove legacy ~/.npm-global directory (replaced by pnpm)"
  '';

  # Additional Claude Code MCP wiring is handled via programs.claude-code.
}
