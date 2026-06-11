# Gecko - headless CLI-only NixOS VM (Proxmox)
#
# Runs Claude Code and OpenAI Codex. No GUI, no SOPS (yet).
#
# To apply: sudo nixos-rebuild switch --flake ~/code/ducktape#gecko
{
  pkgs,
  pkgsUnstable,
  lib,
  ...
}:
{
  imports = [
    ../modules/neovim.nix
    ../modules/shell.nix
    ../modules/tmux.nix
    ../modules/solarized.nix
  ];

  home.username = "agentydragon";
  home.homeDirectory = "/home/agentydragon";
  home.stateVersion = "25.11";

  programs.home-manager.enable = true;

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
  };

  programs.git = {
    enable = true;
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
      credential.helper = "cache";
      "url \"git@github.com:\"" = {
        insteadOf = [
          "https://github.com"
          "https://github.com/"
        ];
      };
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
    pinentry.package = pkgs.pinentry-curses;
  };

  services.ssh-agent.enable = true;

  # TODO: Provision API keys via SOPS (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)
  # Create secrets/home/gecko/ and add ducktape.sopsEnv entries.

  home.packages = with pkgs; [
    pkgsUnstable.claude-code
    pkgsUnstable.codex

    jq
    yq
    fd
    ripgrep
    fzf
    htop
    tree
    pv
    curl
    wget
    gh
    direnv
    git
  ];
}
