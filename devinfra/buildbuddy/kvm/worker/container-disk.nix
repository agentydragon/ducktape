{
  pkgs,
}:
let
  disk = import ./root-disk.nix {
    inherit pkgs;
    sourceImage = import ./source-image.nix { inherit pkgs; };
  };
  rootDiskCheck = import ./root-disk-check.nix {
    inherit pkgs;
    inherit disk;
  };
in
pkgs.dockerTools.buildImage {
  name = "git.allegedly.works/ducktape-ci/buildbuddy-kvm-worker";
  copyToRoot = disk;
  extraCommands = ''
    # Keep the boot check in this image derivation without adding its output to the OCI layer.
    test -s ${rootDiskCheck}
  '';
  uid = 107;
  gid = 107;
  config = { };
}
