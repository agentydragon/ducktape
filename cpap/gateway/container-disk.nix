# Stateless KubeVirt containerDisk for the CPAP gateway VM.
{
  self,
  pkgs,
}:

let
  diskImage = self.nixosConfigurations.cpap-gateway.config.system.build.images.qemu-efi;
  diskRoot = pkgs.runCommand "cpap-gateway-container-disk-root" { } ''
    mkdir -p $out/disk
    cp ${diskImage}/*.qcow2 $out/disk/disk.qcow2
  '';
in
pkgs.dockerTools.buildLayeredImage {
  name = "ghcr.io/agentydragon/cpap-gateway";
  contents = [ diskRoot ];
  includeStorePaths = false;
  maxLayers = 2;
  # streamLayeredImage assembles contents through symlinkJoin;
  # dereference the build-time link before the layer is tarred.
  extraCommands = ''
    cp --dereference disk/disk.qcow2 disk/disk.qcow2.real
    rm disk/disk.qcow2
    mv disk/disk.qcow2.real disk/disk.qcow2
  '';
  fakeRootCommands = ''
    chown 107:107 disk/disk.qcow2
  '';
  # KubeVirt's virt-launcher runs qemu as UID 107. dockerTools
  # applies this ownership to the layer without chowning the Nix
  # store output itself.
  uid = 107;
  gid = 107;
  # The disk's store path changes only with the guest's own inputs, so the publish
  # workflow skips pushing when the newest published image carries the same one:
  # each new tag restarts the VM, and unrelated artifact-pin bumps would otherwise do so.
  config.Labels."works.allegedly.nix-source" = "${diskImage}";
}
