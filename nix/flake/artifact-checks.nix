# A cached marker proves the package build (including import/install checks)
# succeeded without pulling that package's runtime closure onto each CI runner.
{
  pkgs,
  artifacts,
  packages,
}:
let
  registry = builtins.fromJSON (builtins.readFile ../../devinfra/ci/artifact_targets.json);
  availableReleases = pkgs.lib.unique (
    pkgs.lib.mapAttrsToList (_: pin: pin.release) (
      pkgs.lib.filterAttrs (name: _: builtins.hasAttr name artifacts) registry.pins
    )
  );
  releases = pkgs.lib.filterAttrs (
    name: release: release.nixPackage && builtins.elem name availableReleases
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
