# Ducktape devshell. `.envrc` loads it as `path:./nix`, so a direnv reload
# hashes and copies only nix/, not the whole repository. The root flake follows
# these inputs and re-exports the same devShell for `nix develop`.
#
# Bump inputs here (`nix flake update --flake path:./nix`), then run
# `nix flake update devshell` at the repository root.
{
  inputs = {
    # NixOS 26.05 stable release
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    # Unstable for packages that need frequent updates (e.g., claude-code)
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";

    fenix = {
      url = "github:nix-community/fenix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      nixpkgs-unstable,
      fenix,
      ...
    }:
    let
      system = "x86_64-linux";
      packageSets = import ./flake/package_sets.nix {
        inherit
          nixpkgs
          nixpkgs-unstable
          fenix
          system
          ;
      };
    in
    {
      devShells.${system}.default =
        let
          inherit (packageSets) pkgs devTools;
        in
        pkgs.mkShell {
          # Keep each Python CLI's dependencies in its own wrapper, not the shared shell.
          dontAddPythonPath = "1";
          packages = devTools.devToolPackages ++ devTools.localOnlyPackages ++ devTools.systemLibs.packages;
          inherit (devTools.systemLibs) buildInputs;
          LD_LIBRARY_PATH = devTools.systemLibs.libraryPath;
        };
    };
}
