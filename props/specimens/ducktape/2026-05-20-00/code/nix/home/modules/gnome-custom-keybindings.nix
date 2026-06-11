# Auto-generates the GNOME custom-keybindings registry from modular declarations.
#
# Each module declares its bindings via ducktape.gnomeCustomKeybindings:
#   ducktape.gnomeCustomKeybindings.my-shortcut = {
#     name = "My Shortcut";
#     command = "my-command";
#     binding = "<Primary><Alt>x";
#   };
#
# This module collects all declarations and generates both the individual
# dconf keybinding entries and the registry list that GNOME requires.
{ lib, config, ... }:
let
  cfg = config.ducktape.gnomeCustomKeybindings;
  mediaKeys = "org/gnome/settings-daemon/plugins/media-keys";
in
{
  options.ducktape.gnomeCustomKeybindings = lib.mkOption {
    type = lib.types.attrsOf (
      lib.types.submodule {
        options = {
          name = lib.mkOption { type = lib.types.str; };
          command = lib.mkOption { type = lib.types.str; };
          binding = lib.mkOption { type = lib.types.str; };
        };
      }
    );
    default = { };
  };

  config.dconf.settings = {
    ${mediaKeys} = {
      custom-keybindings = map (n: "/${mediaKeys}/custom-keybindings/${n}/") (builtins.attrNames cfg);
    };
  }
  // lib.mapAttrs' (
    name: value:
    lib.nameValuePair "${mediaKeys}/custom-keybindings/${name}" {
      inherit (value) name command binding;
    }
  ) cfg;
}
