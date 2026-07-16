# Shared tmux configuration
{
  pkgs,
  solarizedDark,
  solarizedLight,
  ...
}:
let
  # Generate a tmux status-bar theme from a nix-colors base16 scheme.
  # base00–07 flip between light and dark while the accents (base08–0F) stay
  # fixed, so one template yields both themes. The @sol_* options are consumed
  # by status-left/right in tmux.conf.
  mkTheme =
    scheme:
    let
      p = scheme.palette;
    in
    ''
      set -g @sol_accent "#${p.base0D}"
      set -g @sol_muted "#${p.base02}"
      set -g @sol_fg "#${p.base04}"

      set -g status-style "bg=#${p.base01},fg=#${p.base04}"
      set -g window-status-style "fg=#${p.base04},bg=#${p.base01}"
      set -g window-status-current-style "fg=#${p.base01},bg=#${p.base0D},bold"
      set -g window-status-activity-style "fg=#${p.base0A},bg=#${p.base01}"

      set -g pane-border-style "fg=#${p.base02}"
      set -g pane-active-border-style "fg=#${p.base0D}"

      set -g message-style "bg=#${p.base0A},fg=#${p.base01}"
      set -g message-command-style "bg=#${p.base0A},fg=#${p.base01}"
      set -g mode-style "bg=#${p.base0A},fg=#${p.base01}"

      set -g @prefix_highlight_fg "#${p.base01}"
      set -g @prefix_highlight_bg "#${p.base0A}"
    '';
  darkTheme = pkgs.writeText "tmux-solarized-dark.conf" (mkTheme solarizedDark);
  lightTheme = pkgs.writeText "tmux-solarized-light.conf" (mkTheme solarizedLight);
in
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

    extraConfig = builtins.readFile ./tmux.conf + ''

      # Solarized status bar, auto-switched to match the terminal's light/dark
      # theme via DECSET 2031 reporting: tmux tracks #{client_theme} and fires
      # the client-{dark,light}-theme hooks when the terminal notifies a change.
      # Palettes are generated from nix-colors (see mkTheme in tmux.nix).
      set-hook -g client-dark-theme "source-file ${darkTheme}"
      set-hook -g client-light-theme "source-file ${lightTheme}"
      if-shell -F "#{==:#{client_theme},light}" "source-file ${lightTheme}" "source-file ${darkTheme}"
    '';
  };
}
