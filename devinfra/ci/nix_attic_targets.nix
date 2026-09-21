# The derivations nix-attic-push builds + pushes, as two attrsets consumed by
# flake.nix as `atticPushTargets.<system>.{main,public}`. nix-fast-build
# evaluates these in parallel (nix-eval-jobs) and `--skip-cached` builds/pushes
# only paths missing from the cache. See devinfra/ci/nix_attic_build_and_push.sh.
#
#   main   — every target: all cache-eligible ducktape package outputs (including
#            the full shared Python lockfile closure any of them pulls in — see
#            #3298/#7078) + all NixOS toplevels + home activationPackages +
#            bootstrap packages. The parked codex-pod image remains a flake
#            output, but is excluded from this CI cache.
#            Pushed to the broadly readable `main` cache.
#   public — the bootstrap subset only, also pushed to the anonymous `public`
#            cache (a fresh Claude Code web session substitutes these before any
#            credential exists).
#
# ducktapePkgs only, never gafferPkgs: the caller passes exactly the public
# `./nix/packages` attrset, not the private `./nix/packages/gaffer.nix` one, so
# gaffer-private's own packages never enter the broadly readable `main` cache —
# same boundary the drivefs isolation below enforces for the one leak vector
# through a NixOS/home closure. See cluster/docs/nix_cache.md
# "Private-binary isolation (drivefs)".
#
# drivefs isolation: force services.google-drive off for any home-manager user
# that enables it (currently wyrm2, rugged), so the private gaffer drivefs
# closure never enters the broadly readable `main` cache. Those hosts pull
# drivefs straight from the restricted `gaffer` cache at `nixos-rebuild switch`.
# Reading the enable bool from OUTSIDE the module (via config) avoids the
# infinite recursion an in-module options-existence guard would cause.
# attrByPath returns the default when any path segment is absent — covering
# hosts/users where the option (or home-manager itself) isn't declared, where a
# direct read would throw (and tryEval would NOT catch it: it only traps
# `throw`/`assert`, not attribute-missing errors).
{
  self,
  lib,
  system,
  ducktapePkgs,
}:
let
  gdriveEnabled = cfg: lib.attrByPath [ "services" "google-drive" "enable" ] false cfg;

  forceGdriveOff = { lib, ... }: { services.google-drive.enable = lib.mkForce false; };

  nixosToplevel =
    name:
    let
      cfg = self.nixosConfigurations.${name};
      users = lib.attrByPath [ "home-manager" "users" ] { } cfg.config;
      anyGdrive = lib.any (u: gdriveEnabled users.${u}) (builtins.attrNames users);
      final =
        if anyGdrive then
          cfg.extendModules { modules = [ { home-manager.sharedModules = [ forceGdriveOff ]; } ]; }
        else
          cfg;
    in
    final.config.system.build.toplevel;

  # Force nixGL off for every home target: nixGL's `builtins.currentTime` driver
  # sniffing needs impure eval, which nix-fast-build can't do (it has no --impure
  # and pure-eval=false doesn't expose currentTime). Deterministic no-op for
  # isNixOS/headless configs (nixGL already off there); on atlas it drops the thin
  # OpenGL wrappers, which rebuild at its real `home-manager switch --impure`.
  homeActivation =
    name:
    let
      cfg = self.homeConfigurations.${name};
      final = cfg.extendModules {
        specialArgs = {
          nixGLEnabled = false;
        };
        modules = lib.optional (gdriveEnabled cfg.config) forceGdriveOff;
      };
    in
    final.activationPackage;

  # Bootstrap tool environments used by CI and agent hosts before validation works.
  bootstrap =
    lib.genAttrs [ "bb" "bbr" "bbapi" "devtools" "precommit" "agent-haku" ] (
      n: self.packages.${system}.${n}
    )
    // {
      devShell = self.devShells.${system}.default;
    };

  # Keep the parked image output without making it a CI cache target.
  atticPackages = builtins.removeAttrs ducktapePkgs [ "codex-pod-image" ];

  prefix = p: lib.mapAttrs' (n: v: lib.nameValuePair "${p}-${n}" v);
in
{
  main =
    prefix "pkg" atticPackages
    // prefix "nixos" (lib.genAttrs (builtins.attrNames self.nixosConfigurations) nixosToplevel)
    // prefix "home" (lib.genAttrs (builtins.attrNames self.homeConfigurations) homeActivation)
    // prefix "bootstrap" bootstrap;

  public = prefix "bootstrap" bootstrap;
}
