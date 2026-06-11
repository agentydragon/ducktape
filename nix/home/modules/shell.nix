# Shared shell configuration (zsh, bash, readline, dircolors, aliases, session vars, direnv, zoxide, eza)
{ pkgs, lib, ... }:
let
  commonShellInit = builtins.readFile ../shell/common-init.sh;
  bashInit = builtins.readFile ../shell/bash-init.sh;
  zshInit = builtins.readFile ../shell/zsh-init.zsh;
in
{
  programs.zsh = {
    enable = true;

    envExtra = "skip_global_compinit=1";

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

  programs.bash = {
    enable = true;
    enableCompletion = true;

    shellOptions = [
      "checkwinsize"
      "globstar"
    ];

    initExtra = bashInit + "\n" + commonShellInit;
  };

  programs.readline = {
    enable = true;
    variables = {
      show-all-if-ambiguous = true;
    };
  };

  programs.dircolors.enable = true;

  home.shellAliases = {
    ".." = "cd ..";
    suspend = "systemctl suspend";
    npm = "pnpm";
    npx = "echo '❌ No you idiot, use pnpm dlx' && false";
    gmrc = "glab mr create --fill --remove-source-branch --yes";
    gs = "git status --short --branch --show-stash";
    vimdiff = "nvim -d";
    alert = ''notify-send --urgency=low -i "$([ $? = 0 ] && echo terminal || echo error)" "$(history|tail -n1|sed -e 's/^\s*[0-9]\+\s*//;s/[;&|]\s*alert$//')"'';

    lt = "eza -l --tree --icons=auto --group-directories-first";
    lS = "eza -l --sort=size --reverse --icons=auto --group-directories-first";
    ld = "eza -l --only-dirs --icons=auto --group-directories-first";
    l1 = "eza -1 --icons=auto";
    lm = "eza -l --sort=modified --reverse --icons=auto --group-directories-first";
  };

  home.sessionVariables = {
    EDITOR = "nvim";
    VISUAL = "nvim";

    DEFAULT_CHARSET = "utf8";

    GCC_COLORS = "error=01;31:warning=01;35:note=01;36:caret=01;32:locus=01:quote=01";

    LESS = "-F -X -R";

    GOPATH = "$HOME/.go";

    PNPM_HOME = "$HOME/.local/share/pnpm";
  };

  programs.direnv = {
    enable = true;
    enableBashIntegration = true;
    enableZshIntegration = true;
    nix-direnv.enable = true;
  };

  programs.zoxide = {
    enable = true;
    enableBashIntegration = false;
    enableZshIntegration = false;
    options = [ "--cmd cd" ];
  };

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

  home.file.".p10k.zsh".source = ../p10k.zsh;
}
