# Stateless NixOS qcow2 packaged as a KubeVirt containerDisk.
{
  pkgs,
  self,
}:
let
  diskImage =
    self.nixosConfigurations.buildbuddy-kvm-worker-spike.config.system.build.images.qemu-efi;
  diskRoot = pkgs.runCommand "buildbuddy-kvm-worker-spike-container-disk-root" { } ''
    mkdir -p "$out/disk"
    cp ${diskImage}/*.qcow2 "$out/disk/disk.qcow2"
  '';
in
pkgs.dockerTools.buildLayeredImage {
  name = "git.allegedly.works/ducktape-ci/buildbuddy-kvm-worker-spike";
  contents = [ diskRoot ];
  includeStorePaths = false;
  maxLayers = 2;
  extraCommands = ''
    cp --dereference disk/disk.qcow2 disk/disk.qcow2.real
    rm disk/disk.qcow2
    mv disk/disk.qcow2.real disk/disk.qcow2
  '';
  fakeRootCommands = ''
    chown 107:107 disk/disk.qcow2
  '';
  uid = 107;
  gid = 107;
  config.Labels."works.allegedly.nix-source" = "${diskImage}";
}
