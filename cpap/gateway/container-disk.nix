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
  config = { };
}
