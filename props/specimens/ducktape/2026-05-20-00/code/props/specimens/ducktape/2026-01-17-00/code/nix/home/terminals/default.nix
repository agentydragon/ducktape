{
  config,
  pkgs,
  lib,
  enableGui,
  solarizedLight,
  solarizedDark,
  terminalFont,
  nixGLPackages,
  ...
}: let
  lightScheme = solarizedLight;
  darkScheme = solarizedDark;
  fontFamily = terminalFont.family;
  fontSizeValue = terminalFont.size;
  fontSizeStr = builtins.toString fontSizeValue;

  kittyPkg = config.lib.nixGL.wrap pkgs.kitty;
  weztermPkg = config.lib.nixGL.wrap pkgs.wezterm;
  ghosttyPkg = config.lib.nixGL.wrap pkgs.ghostty;

  mkKittyTheme = scheme: isLight: let
    p = scheme.palette;
    colors =
      if isLight
      then [
        p.base01
        p.base08
        p.base0B
        p.base0A
        p.base0D
        p.base0E
        p.base0C
        p.base07
        p.base00
        p.base09
        p.base02
        p.base03
        p.base04
        p.base0F
        p.base05
        p.base06
      ]
      else [
        p.base01
        p.base08
        p.base0B
        p.base0A
        p.base0D
        p.base0E
        p.base0C
        p.base06
        p.base00
        p.base09
        p.base02
        p.base03
        p.base04
        p.base0F
        p.base05
        p.base07
      ];
    colorLines = lib.concatStringsSep "
" (lib.imap0 (idx: hex: "color${toString idx} #${hex}") colors);
    selectionFg =
      if isLight
      then p.base07
      else p.base06;
  in ''
    background #${p.base00}
    foreground #${p.base05}
    selection_background #${p.base01}
    selection_foreground #${selectionFg}
    cursor #${p.base05}
    ${colorLines}
  '';

  mkColorList = colors:
    "{ "
    + lib.concatStringsSep ", " (builtins.map (hex: "\"#${hex}\"") colors)
    + " }";

  mkWeztermScheme = scheme: isLight: let
    p = scheme.palette;
    ansi = [
      p.base02
      p.base08
      p.base0B
      p.base0A
      p.base0D
      p.base0E
      p.base0C
      (
        if isLight
        then p.base07
        else p.base05
      )
    ];
    brights = [
      p.base03
      p.base08
      p.base0B
      p.base0A
      p.base0D
      p.base0E
      p.base0C
      (
        if isLight
        then p.base06
        else p.base07
      )
    ];
    selectionFg =
      if isLight
      then p.base05
      else p.base06;
  in ''
    {
      foreground = "#${p.base05}";
      background = "#${p.base00}";
      cursor_bg = "#${p.base05}";
      cursor_fg = "#${p.base00}";
      cursor_border = "#${p.base05}";
      selection_bg = "#${p.base02}";
      selection_fg = "#${selectionFg}";
      scrollbar_thumb = "#${p.base02}";
      split = "#${p.base01}";
      ansi = ${mkColorList ansi};
      brights = ${mkColorList brights};
    }
  '';

  kittyApplyScript = ./scripts/kitty-apply-theme.sh;
  kittyWatcherScript = ./scripts/kitty-theme-watcher.sh;
  weztermConfig = let
    darkSchemeLua = mkWeztermScheme darkScheme false;
    lightSchemeLua = mkWeztermScheme lightScheme true;
  in ''
    local wezterm = require 'wezterm'
    local act = wezterm.action

    local function scheme_for_appearance(appearance)
      if appearance:find 'Dark' then
        return 'Solarized (dark)'
      end
      return 'Solarized (light)'
    end

      local config = {
        check_for_updates = false,
        enable_scroll_bar = true,
        hide_tab_bar_if_only_one_tab = false,
        use_fancy_tab_bar = false,
        scroll_to_bottom_on_input = true,
        font = wezterm.font '${fontFamily}',
        font_size = ${fontSizeStr},
        color_schemes = {
          ["Solarized (dark)"] = ${darkSchemeLua},
          ["Solarized (light)"] = ${lightSchemeLua},
        },
        keys = {
          { key = "t", mods = "CTRL|SHIFT", action = act.SpawnTab "CurrentPaneDomain" },
          { key = "n", mods = "CTRL|SHIFT", action = act.SpawnWindow },
          { key = "w", mods = "CTRL|SHIFT", action = act.CloseCurrentTab { confirm = false } },
          { key = "PageUp", mods = "CTRL", action = act.ActivateTabRelative(-1) },
          { key = "PageDown", mods = "CTRL", action = act.ActivateTabRelative(1) },
          { key = "PageUp", mods = "CTRL|SHIFT", action = act.MoveTabRelative(-1) },
          { key = "PageDown", mods = "CTRL|SHIFT", action = act.MoveTabRelative(1) },
          { key = "PageUp", mods = "CTRL|SHIFT", action = act.MoveTabRelative(-1) },
          { key = "PageDown", mods = "CTRL|SHIFT", action = act.MoveTabRelative(1) },
          { key = "c", mods = "CTRL|SHIFT", action = act.CopyTo "ClipboardAndPrimarySelection" },
          { key = "v", mods = "CTRL|SHIFT", action = act.PasteFrom "Clipboard" },
          { key = "f", mods = "CTRL|SHIFT", action = act.Search "CurrentSelectionOrEmptyString" },
      },
    }

    if wezterm.gui then
      config.color_scheme = scheme_for_appearance(wezterm.gui.get_appearance())
    else
      config.color_scheme = 'Solarized (dark)'
    end

    wezterm.on('window-config-reloaded', function(window)
      local overrides = window:get_config_overrides() or {}
      local appearance = window:get_appearance()
      local scheme = scheme_for_appearance(appearance)
      if overrides.color_scheme ~= scheme then
        overrides.color_scheme = scheme
        window:set_config_overrides(overrides)
      end
    end)

    return config
  '';
  ghosttyConfigText = ''
    theme = dark:Builtin Solarized Dark,light:Builtin Solarized Light
    scrollbar = system
    tabs = true
    scroll-to-bottom = keystroke,no-output
    font-family = "${fontFamily}"
    font-size = ${fontSizeStr}
    keybind = ctrl+shift+t=new_tab
    keybind = ctrl+shift+n=new_window
    keybind = ctrl+shift+w=close_surface
    keybind = ctrl+page_up=previous_tab
    keybind = ctrl+page_down=next_tab
    keybind = ctrl+shift+page_up=move_tab:-1
    keybind = ctrl+shift+page_down=move_tab:1
    keybind = ctrl+shift+c=copy_to_clipboard
    keybind = ctrl+shift+v=paste_from_clipboard
    keybind = ctrl+shift+f=search
  '';
in {
  config = lib.mkIf enableGui {
    targets.genericLinux.nixGL.packages = nixGLPackages;

    programs.wezterm = {
      enable = true;
      package = weztermPkg;
      extraConfig = weztermConfig;
    };

    programs.kitty = {
      enable = true;
      package = kittyPkg;
      extraConfig = ''
        include ~/.config/kitty/current-theme.conf
        map ctrl+shift+t new_tab
        map ctrl+shift+n new_os_window
        map ctrl+shift+w close_tab
        map ctrl+page_up previous_tab
        map ctrl+page_down next_tab
        map ctrl+shift+page_up move_tab_backward
        map ctrl+shift+page_down move_tab_forward
        map ctrl+shift+c copy_to_clipboard
        map ctrl+shift+v paste_from_clipboard
        map ctrl+shift+f enter_search
      '';
      settings = {
        scrollback_lines = 20000;
        font_family = "${fontFamily}";
        font_size = "${fontSizeStr}";
        enable_audio_bell = "no";
        allow_remote_control = "yes";
        listen_on = "unix:/tmp/kitty-remote";
        confirm_os_window_close = "0";
      };
    };

    xdg.configFile."kitty/themes/solarized-dark.conf".text = mkKittyTheme darkScheme false;
    xdg.configFile."kitty/themes/solarized-light.conf".text = mkKittyTheme lightScheme true;

    xdg.configFile."ghostty/config".text = ghosttyConfigText;

    home.file.".config/kitty/bin/kitty-apply-theme.sh" = {
      source = kittyApplyScript;
      executable = true;
    };

    home.file.".config/kitty/bin/kitty-theme-watcher.sh" = {
      source = kittyWatcherScript;
      executable = true;
    };

    systemd.user.services."kitty-theme-watcher" = {
      Unit = {
        Description = "Sync kitty theme with GNOME color preference";
        After = ["graphical-session.target"];
        PartOf = ["graphical-session.target"];
      };
      Service = {
        ExecStart = "${pkgs.bash}/bin/bash ${config.xdg.configHome}/kitty/bin/kitty-theme-watcher.sh";
        Restart = "on-failure";
      };
      Install = {
        WantedBy = ["graphical-session.target"];
      };
    };

    home.packages = [
      ghosttyPkg
      nixGLPackages.nixGLDefault
    ];
  };

  # TODO(agentydragon): Manually test kitty-theme-watcher by toggling GNOME dark style and verifying running windows update.
}
