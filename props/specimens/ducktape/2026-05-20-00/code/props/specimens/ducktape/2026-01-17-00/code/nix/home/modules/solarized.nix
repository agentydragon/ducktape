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
}: let
  solarizedLightScheme = solarizedLight;
  solarizedDarkScheme = solarizedDark;
in {
  # Install Night Theme Switcher extension and theme switching utility
  home.packages = with pkgs;
    [
      # Required system libraries (always needed for other tools)
      gobject-introspection
      glib
    ]
    ++ lib.optionals enableGui [
      gnomeExtensions.night-theme-switcher # ID 2236: Night Theme Switcher
      # TODO: Add gnome-terminal-profile-switcher once wheel is published to GitHub Releases
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

  # Midnight Commander Solarized configuration
  xdg.configFile."mc/solarized.ini" = {
    source = ../mc-solarized.ini;
  };

  # GNOME Terminal Solarized profiles using nix-colors schemes (GUI only)
  # This creates both profiles which can be switched dynamically with switch_gnome_terminal_profile
  programs.gnome-terminal = lib.mkIf enableGui {
    enable = true;
    showMenubar = false;

    profile = let
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
      builtins.mapAttrs (_: profile:
        profile
        // {
          scrollOnOutput = false;
          font = fontString;
        })
      baseProfiles;
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

  dconf.settings = lib.mkIf enableGui {
    # Set default terminal
    "org/gnome/desktop/applications/terminal" = {
      exec = "gnome-terminal.wrapper";
      exec-arg = lib.hm.gvariant.mkNothing lib.hm.gvariant.type.string; # Unset the argument
    };

    # GNOME Shell extension management
    "org/gnome/shell" = {
      # Enable night-theme-switcher extension
      enabled-extensions = [
        "nightthemeswitcher@romainvigier.fr" # Night Theme Switcher
      ];
    };

    # Night Theme Switcher extension settings
    "org/gnome/shell/extensions/nightthemeswitcher/commands" = {
      enabled = true;
      sunrise = "switch_gnome_terminal_profile --profile='Solarized Light'";
      sunset = "switch_gnome_terminal_profile --profile='Solarized Dark'";
    };
  };
}
