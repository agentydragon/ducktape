# A cached marker proves the package build (including import/install checks)
# succeeded without pulling that package's runtime closure onto each CI runner.
{ pkgs, packages }:
let
  registry = builtins.fromJSON (builtins.readFile ../../devinfra/ci/artifact_targets.json);
  releases = pkgs.lib.filterAttrs (
    name: release: release.nixPackage && builtins.hasAttr name packages
  ) registry.releases;
in
pkgs.lib.mapAttrs' (
  name: _:
  pkgs.lib.nameValuePair "artifact-${name}" (
    pkgs.runCommand "${name}-artifact-check"
      {
        checkedPackage = packages.${name};
        allowedReferences = [ ];
      }
      ''
        test -e "$checkedPackage"
        touch "$out"
      ''
  )
) releases
