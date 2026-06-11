# NixOS module for heavy packages
#
# Usage in your NixOS configuration.nix:
#   imports = [ /path/to/ducktape/nix/nixos/heavy-packages-module.nix ];
#   programs.heavyPackages.enable = true;
#
# This installs the same packages that home-manager would install on non-NixOS systems,
# but at the system level for better sharing and reduced duplication.
{
  config,
  lib,
  pkgs,
  ...
}: let
  # Import the single source of truth for heavy packages
  heavyPkgs = import ../home/heavy-packages.nix;
in {
  options.programs.heavyPackages = {
    enable = lib.mkEnableOption "heavy creative and CAD packages (GIMP, Krita, FreeCAD, Inkscape, etc.)";
  };

  config = lib.mkIf config.programs.heavyPackages.enable {
    environment.systemPackages = heavyPkgs.heavyPackages pkgs;
  };
}
