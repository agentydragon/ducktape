# Fails evaluation when flake.lock's copy of the devshell flake's inputs (taken
# from nix/flake.lock at the last `nix flake update devshell`) has drifted from
# nix/flake.lock: `nix develop` and hosts would then build from different
# nixpkgs/fenix revisions than direnv.
{
  pkgs,
  rootLock,
  devshellLock,
}:
let
  inherit (pkgs) lib;
  # A lock input is a node name, or a follows path from that lock's root.
  resolve =
    lock: path:
    lib.foldl' (
      node: name:
      let
        ref = node.inputs.${name};
      in
      if builtins.isList ref then resolve lock ref else lock.nodes.${ref}
    ) lock.nodes.${lock.root} path;
  # Input paths reachable through direct edges; follows edges alias one of these.
  inputPaths =
    prefix: node:
    lib.concatLists (
      lib.mapAttrsToList (
        name: ref:
        lib.optionals (!builtins.isList ref) (
          [ (prefix ++ [ name ]) ] ++ inputPaths (prefix ++ [ name ]) devshellLock.nodes.${ref}
        )
      ) (node.inputs or { })
    );
  stale = builtins.filter (
    path: (resolve devshellLock path).locked != (resolve rootLock ([ "devshell" ] ++ path)).locked
  ) (inputPaths [ ] devshellLock.nodes.${devshellLock.root});
in
if stale == [ ] then
  pkgs.runCommand "devshell-lock-sync" { } "touch $out"
else
  throw "flake.lock is out of sync with nix/flake.lock for ${
    lib.concatMapStringsSep ", " (lib.concatStringsSep "/") stale
  }; run `nix flake update devshell` at the repository root."
