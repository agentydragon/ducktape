# Ephemeral KubeVirt containerDisk for public-coder-devbox.
#
# The VM boots straight into its build/test configuration and owns first-boot
# disk setup through NixOS systemd units. This suits containerDisk's
# "every boot is a fresh disk" model: Bazel/BuildBuddy caches are remote, and
# the VM is intended to run current devel tooling rather than carry state.
{
  self,
  pkgs,
}:

let
  diskImage = self.nixosConfigurations.public-coder-devbox.config.system.build.images.qemu-efi;
  diskRoot = pkgs.runCommand "public-coder-devbox-container-disk-root" { } ''
    mkdir -p $out/disk
    cp ${diskImage}/*.qcow2 $out/disk/disk.qcow2
  '';
in
pkgs.dockerTools.buildLayeredImage {
  name = "git.allegedly.works/ducktape-ci/public-coder-devbox";
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
  config = { };
}
