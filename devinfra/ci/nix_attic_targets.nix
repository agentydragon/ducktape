# Every Nix output nix-attic-push builds + pushes, aggregated into ONE linkFarm
# derivation so CI evaluates the flake exactly once. Building the linkFarm
# realises all targets (Nix substitutes/builds them in parallel); the caller
# reads the named symlinks back to recover per-target store paths for `attic
# push` (bootstrap-* entries are the subset that also goes to the public cache).
#
# drivefs isolation: force services.google-drive off for any home-manager user
# that enables it (currently wyrm2, rugged), so the private gaffer drivefs
# closure never enters the broadly readable `main` cache. Those hosts pull
# drivefs straight from the restricted `gaffer` cache at `nixos-rebuild switch`.
# See cluster/docs/nix_cache.md "Private-binary isolation (drivefs)". Reading
# the enable bool from OUTSIDE the module (via config) avoids the infinite
# recursion an in-module options-existence guard would cause. attrByPath returns
# the default when any path segment is absent — covering hosts/users where the
# option (or home-manager itself) isn't declared, where a direct read would
# throw (and tryEval would NOT catch it: it only traps `throw`/`assert`, not
# attribute-missing errors).
{ flakePath }:
let
  flake = builtins.getFlake flakePath;
  system = "x86_64-linux";
  pkgs = flake.inputs.nixpkgs.legacyPackages.${system};
  inherit (pkgs) lib;

  gdriveEnabled = cfg: lib.attrByPath [ "services" "google-drive" "enable" ] false cfg;

  forceGdriveOff = { lib, ... }: { services.google-drive.enable = lib.mkForce false; };

  nixosToplevel =
    name:
    let
      cfg = flake.nixosConfigurations.${name};
      users = lib.attrByPath [ "home-manager" "users" ] { } cfg.config;
      anyGdrive = lib.any (u: gdriveEnabled users.${u}) (builtins.attrNames users);
      final =
        if anyGdrive then
          cfg.extendModules { modules = [ { home-manager.sharedModules = [ forceGdriveOff ]; } ]; }
        else
          cfg;
    in
    final.config.system.build.toplevel;

  homeActivation =
    name:
    let
      cfg = flake.homeConfigurations.${name};
      final =
        if gdriveEnabled cfg.config then cfg.extendModules { modules = [ forceGdriveOff ]; } else cfg;
    in
    final.activationPackage;

  # Bootstrap tools agent hosts need before BuildBuddy-backed validation works.
  # Keep in sync with the public-cache push in nix_attic_build_and_push.sh.
  bootstrapEntries =
    map
      (n: {
        name = "bootstrap-${n}";
        path = flake.packages.${system}.${n};
      })
      [
        "bb"
        "bbr"
        "bbapi"
        "devtools"
        "agent-haku"
      ]
    ++ [
      {
        name = "bootstrap-devShell";
        path = flake.devShells.${system}.default;
      }
    ];

  entries =
    map (n: {
      name = "nixos-${n}";
      path = nixosToplevel n;
    }) (builtins.attrNames flake.nixosConfigurations)
    ++ map (n: {
      name = "home-${n}";
      path = homeActivation n;
    }) (builtins.attrNames flake.homeConfigurations)
    ++ bootstrapEntries;
in
pkgs.linkFarm "nix-attic-push-targets" entries
