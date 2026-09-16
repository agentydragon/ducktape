{ pkgs, ... }:
{
  environment.packages = [ pkgs.hello ];

  # nix-on-droid's own stateVersion enum, independent of this repo's nixpkgs
  # pin (nixos-26.05) — "24.05" is the newest value it currently accepts.
  system.stateVersion = "24.05";
}
