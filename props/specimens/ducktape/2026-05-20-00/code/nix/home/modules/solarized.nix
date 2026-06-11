# Solarized theming configuration
# GNOME Terminal themes, bat, delta, MC, and automatic light/dark switching
{
  pkgs,
  lib,
  enableGui,
  solarizedLight,
  solarizedDark,
  terminalFont,
  ...
}:
let
  solarizedLightScheme = solarizedLight;
  solarizedDarkScheme = solarizedDark;

  # Theme switching scripts - wrappers around gterm-theme.
  # Installed to PATH via home.packages; called by Night Theme Switcher
  # extension on sunrise/sunset and available for manual use.
  set_light_theme = pkgs.writeShellApplication {
    name = "set_light_theme";
    runtimeInputs = [ ]; # gterm-theme expected on PATH via home.packages
    text = "gterm-theme --profile='Solarized Light'";
  };

  set_dark_theme = pkgs.writeShellApplication {
    name = "set_dark_theme";
    runtimeInputs = [ ]; # gterm-theme expected on PATH via home.packages
    text = "gterm-theme --profile='Solarized Dark'";
  };
in
{
  # Install Night Theme Switcher extension and theme switching utility
  home.packages =
    with pkgs;
    [
      # Required system libraries (always needed for other tools)
      gobject-introspection
      glib
    ]
    ++ [
      set_light_theme
      set_dark_theme
    ];

  # Bat theme environment variables for light/dark mode switching
  home.sessionVariables = {
    BAT_THEME_DARK = "Solarized (dark)";
    BAT_THEME_LIGHT = "Solarized (light)";
    # Default to dark theme
    BAT_THEME = "Solarized (dark)";

    # Midnight Commander skin
    MC_SKIN = "$HOME/.config/mc/solarized.ini";
  };

  # Midnight Commander Solarized skin
  xdg.configFile."mc/solarized.ini".source = (pkgs.formats.ini { }).generate "mc-solarized.ini" {
    skin.description = "Solarized";
    Lines = {
      lefttop = "┌";
      righttop = "┐";
      centertop = "─";
      centerbottom = "─";
      leftbottom = "└";
      rightbottom = "┘";
      leftmiddle = "├";
      rightmiddle = "┤";
      centermiddle = "┼";
      horiz = "─";
      vert = "│";
      thinhoriz = "─";
      thinvert = "│";
    };
    core = {
      _default_ = "lightgray;black";
      selected = "white;blue";
      marked = "white";
      markselect = "brightred;blue";
      gauge = ";yellow";
      input = "black;brown";
      reverse = "blue;green";
    };
    dialog = {
      _default_ = "black;lightgray";
      dfocus = "black;green";
      dhotnormal = "blue;lightgray";
      dhotfocus = "blue;green";
    };
    error = {
      _default_ = "white;red";
      errdhotnormal = "brightgreen;";
      errdhotfocus = "blue;green";
    };
    filehighlight = {
      directory = "cyan;";
      executable = "brightred;";
      symlink = "magenta;";
      stalelink = "lightgray;red";
      device = "brown;blue";
      special = "black;blue";
      core = "brightcyan;";
      temp = "brightgreen;";
      archive = "brightmagenta;";
      doc = "red;";
      source = "green;";
      media = "brown;";
      graph = "blue;";
      database = ";";
    };
    menu = {
      _default_ = "black;lightgray";
      menuhot = "brightred;";
      menusel = "blue;green";
      menuhotsel = "brightmagenta;green";
      menuinactive = "lightgray;black";
    };
    help = {
      _default_ = "lightgray;blue";
      helpitalic = "gray;";
      helpbold = "white;";
      helplink = "brown;";
      helpslink = "brightmagenta;green";
    };
    editor = {
      _default_ = "lightgray;black";
      editbold = "green;blue";
      editmarked = "lightgray;green";
      editwhitespace = "brightblue;blue";
      editlinestate = "brightmagenta";
      bookmark = "white;red";
      bookmarkfound = "black;green";
    };
    viewer.viewunderline = "brighmagenta;black";
    buttonbar = {
      hotkey = "lightgray;black";
      button = "white;blue";
    };
    widget-common = {
      sort-sign-up = "↓";
      sort-sign-down = "↑";
    };
    widget-panel = {
      hiddenfiles-sign-show = "⋅";
      hiddenfiles-sign-hide = "•";
      history-prev-item-sign = "«";
      history-next-item-sign = "»";
      history-show-list-sign = "^";
    };
  };

  # GNOME Terminal Solarized profiles using nix-colors schemes (GUI only)
  # This creates both profiles which can be switched dynamically with switch_gnome_terminal_profile
  programs.gnome-terminal = lib.mkIf enableGui {
    enable = true;
    showMenubar = false;

    profile =
      let
        # Helper function to build a terminal palette from a color scheme
        mkTerminalPalette = scheme: [
          "#${scheme.palette.base01}" # black
          "#${scheme.palette.base08}" # red
          "#${scheme.palette.base0B}" # green
          "#${scheme.palette.base09}" # yellow/orange
          "#${scheme.palette.base0D}" # blue
          "#${scheme.palette.base0E}" # magenta
          "#${scheme.palette.base0C}" # cyan
          "#${scheme.palette.base06}" # white
          "#${scheme.palette.base00}" # bright black
          "#${scheme.palette.base08}" # bright red
          "#${scheme.palette.base0B}" # bright green
          "#${scheme.palette.base0A}" # bright yellow
          "#${scheme.palette.base0D}" # bright blue
          "#${scheme.palette.base0F}" # bright magenta (violet)
          "#${scheme.palette.base0C}" # bright cyan
          "#${scheme.palette.base07}" # bright white
        ];

        # Base profile definitions
        baseProfiles = {
          # Solarized Light profile
          "b1dcc9dd-5262-4d8d-a863-c897e6d979b9" = {
            visibleName = "Solarized Light";
            default = true;
            colors = {
              foregroundColor = "#${solarizedLightScheme.palette.base05}";
              backgroundColor = "#${solarizedLightScheme.palette.base07}";
              boldColor = "#${solarizedLightScheme.palette.base04}";
              palette = mkTerminalPalette solarizedLightScheme;
              cursor = {
                foreground = "#${solarizedLightScheme.palette.base07}";
                background = "#${solarizedLightScheme.palette.base05}";
              };
            };
          };

          # Solarized Dark profile
          "5083e06b-024e-46be-9cd2-892b814f1fc8" = {
            visibleName = "Solarized Dark";
            colors = {
              foregroundColor = "#${solarizedDarkScheme.palette.base05}";
              backgroundColor = "#${solarizedDarkScheme.palette.base00}";
              boldColor = "#${solarizedDarkScheme.palette.base06}";
              palette = mkTerminalPalette solarizedDarkScheme;
              cursor = {
                foreground = "#${solarizedDarkScheme.palette.base00}";
                background = "#${solarizedDarkScheme.palette.base05}";
              };
            };
          };
        };
        fontString = "${terminalFont.family} ${builtins.toString terminalFont.size}";
        # Apply common settings to every profile: scroll-on-output=false and shared font
      in
      builtins.mapAttrs (
        _: profile:
        profile
        // {
          scrollOnOutput = false;
          font = fontString;
        }
      ) baseProfiles;
  };

  # Bat configuration with Solarized themes
  programs.bat = {
    enable = true;
    config = {
      # Default theme - can be overridden by BAT_THEME environment variable
      theme = "Solarized (dark)";
    };
  };

  # Delta - better git diffs with Solarized theme
  programs.delta = {
    enable = true;
    enableGitIntegration = true;
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

  # Night Theme Switcher extension — package and enabled-extensions via programs.gnome-shell
  programs.gnome-shell.extensions = lib.mkIf enableGui [
    { package = pkgs.gnomeExtensions.night-theme-switcher; }
  ];

  dconf.settings = lib.mkIf enableGui {
    # Set default terminal
    "org/gnome/desktop/applications/terminal" = {
      exec = "gnome-terminal.wrapper";
      exec-arg = lib.hm.gvariant.mkNothing lib.hm.gvariant.type.string; # Unset the argument
    };

    # Night Theme Switcher extension settings
    "org/gnome/shell/extensions/nightthemeswitcher/commands" = {
      enabled = true;
      sunrise = "set_light_theme";
      sunset = "set_dark_theme";
    };
  };
}
