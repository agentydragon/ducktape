# Shared tmux configuration
{ pkgs, ... }:
{
  programs.tmux = {
    enable = true;
    sensibleOnTop = true;

    mouse = true;
    historyLimit = 100000;
    baseIndex = 1;
    keyMode = "vi";
    clock24 = true;
    prefix = "C-b";
    terminal = "tmux-256color";

    plugins = with pkgs.tmuxPlugins; [
      resurrect
      continuum
      yank
      prefix-highlight
    ];

    extraConfig = builtins.readFile ./tmux.conf;
  };
}
