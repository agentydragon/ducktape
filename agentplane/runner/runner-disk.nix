# The Agentplane-owned executables (the runner and the `agentplane` CLI), in one image with two
# renderings: a read-only erofs at `disk/runner.img`, which the runner VM attaches as a KubeVirt
# containerDisk beside its NixOS root disk (cluster/cdk8s/agentplane/runner_vm.py), and the same
# two files under `bin/`, which a runner Pod mounts as an image volume. A pin sync republishes
# only this image, not the guest or the Pod's tools image.
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
  # A Pod runs `agentplane-runner` directly, so the guest's `agentplane-runner-vm` alias stays in
  # the erofs only.
  podTree = pkgs.runCommand "agentplane-runner-disk-bin" { } ''
    mkdir -p $out/bin
    cp ${agentplaneRunner}/bin/agentplane-runner ${agentplaneCli}/bin/agentplane $out/bin/
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
    cp -a ${podTree}/bin $out/bin
  '';
in
pkgs.dockerTools.buildLayeredImage {
  name = "git.allegedly.works/ducktape-ci/agentplane-runner-disk";
  contents = [ diskRoot ];
  includeStorePaths = false;
  maxLayers = 2;
  extraCommands = ''
    for file in disk/runner.img bin/*; do
      cp --dereference "$file" "$file.real"
      rm "$file"
      mv "$file.real" "$file"
    done
  '';
  fakeRootCommands = ''
    chown 107:107 disk/runner.img
  '';
  uid = 107;
  gid = 107;
  config = { };
}
