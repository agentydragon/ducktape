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
  ...
}:
let
  toTOML = (pkgs.formats.toml { }).generate;

  ducktapePackages = import ../packages {
    inherit lib pkgs pkgsUnstable;
    artifacts = ducktape-artifacts;
  };
  inherit (ducktapePackages)
    ducktape
    claude-hook-rs
    claude-statusline
    gterm-theme
    bbapi
    ;

  tanaClaude = pkgs.writeShellApplication {
    name = "tana-claude";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.kubectl
    ];
    text = builtins.readFile ./scripts/tana-claude.sh;
  };

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
    ./programs/gemini-cli.nix
    ./gemini_cli.nix
    ./shell/oh-my-posh.nix
    ./modules/gnome-custom-keybindings.nix
    ./modules/attic.nix
    ./modules/atuin.nix
    ./modules/buildbuddy.nix
    ./modules/datetime-format.nix
    ./modules/sops-env.nix
    ./services/activitywatch.nix
    ./opencode
    ./modules/gnome-shell-keybindings.nix
    ./modules/flameshot-screenshots.nix
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
  };

  home.username = "agentydragon";
  home.homeDirectory = "/home/agentydragon";

  programs.home-manager.enable = true;

  xdg.userDirs = {
    enable = true;
    createDirectories = false;
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
    matchBlocks = {
      # agent-box VM: `ssh agent-box.allegedly.works` lands as the codex user.
      # Distinct port because gecko owns :22 on the hil nodes (see
      # cluster/k8s/agent-box/app/ciliumenvoyconfig.yaml).
      "agent-box.allegedly.works" = {
        hostname = "agent-box.allegedly.works";
        user = "codex";
        port = 2201;
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
      nodePackages.pnpm
      bun

      rustc
      cargo
      clippy
      rust-analyzer
      sccache
      gcc

      (writeShellScriptBin "z-claude" ''
        exec env \
          ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic \
          ANTHROPIC_AUTH_TOKEN="$ZAI_API_KEY" \
          ANTHROPIC_MODEL=glm-5.2 \
          claude --disallowed-tools "WebFetch WebSearch" \
          "$@"
      '')

      tanaClaude

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

      flameshot
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
      { package = pkgs.gnomeExtensions.panel-date-format; }
      { package = pkgs.gnomeExtensions.cronomix; }
      { package = pkgs.gnomeExtensions.pop-shell; }
      { package = pkgs.gnomeExtensions.gsconnect; }
      { package = pkgs.gnomeExtensions.display-scale-switcher; }
      { package = pkgsUnstable.gnomeExtensions.just-perfection; }
      { package = pkgs.gnomeExtensions.user-themes; }
    ];
  };

  home.file.".cargo/config.toml".source = toTOML "cargo-config.toml" { };

  home.file.".ansible.cfg".source = (pkgs.formats.ini { }).generate "ansible.cfg" {
    defaults.collections_path = "~/.ansible/collections";
  };

  home.activation.warnLegacyNpmGlobal = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    [[ -d "$HOME/.npm-global" ]] && echo "⚠️  WARNING: Remove legacy ~/.npm-global directory (replaced by pnpm)"
  '';
}
