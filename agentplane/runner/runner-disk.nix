# The Agentplane-owned executables (the runner and the `agentplane` CLI) as a read-only erofs,
# packaged as a KubeVirt containerDisk that the runner VM attaches beside its NixOS root disk
# (cluster/cdk8s/agentplane/runner_vm.py). A pin sync republishes only this image, not the guest.
#
# The filesystem has no compression: the registry compresses the layer on the wire, and the guest
# needs no kernel decompressor beyond erofs itself.
{
  pkgs,
  agentplaneRunner,
  agentplaneCli,
}:
let
  tree = pkgs.runCommand "agentplane-runner-disk-tree" { } ''
    mkdir -p $out/bin
    cp -a ${agentplaneRunner}/bin/. $out/bin/
    chmod u+w $out/bin
    cp -a ${agentplaneCli}/bin/. $out/bin/
  '';
  # A fixed timestamp and UUID keep the image a function of the two executables.
  filesystem =
    pkgs.runCommand "agentplane-runner-disk.img" { nativeBuildInputs = [ pkgs.erofs-utils ]; }
      ''
        mkfs.erofs -T0 -U 00000000-0000-0000-0000-000000000000 --all-root -L agentplane $out ${tree}
      '';
  diskRoot = pkgs.runCommand "agentplane-runner-disk-container-disk-root" { } ''
    mkdir -p $out/disk
    cp ${filesystem} $out/disk/runner.img
  '';
in
pkgs.dockerTools.buildLayeredImage {
  name = "git.allegedly.works/ducktape-ci/agentplane-runner-disk";
  contents = [ diskRoot ];
  includeStorePaths = false;
  maxLayers = 2;
  extraCommands = ''
    cp --dereference disk/runner.img disk/runner.img.real
    rm disk/runner.img
    mv disk/runner.img.real disk/runner.img
  '';
  fakeRootCommands = ''
    chown 107:107 disk/runner.img
  '';
  uid = 107;
  gid = 107;
  config = { };
}
