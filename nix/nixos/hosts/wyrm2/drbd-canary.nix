# Host preparation for the two-node canary in cluster/docs/drbd_canary.md.
# LINSTOR will own resources; do not enable the host DRBD resource service.
{ config, pkgs, ... }:
{
  boot.extraModulePackages = [ config.boot.kernelPackages.drbd ];
  boot.kernelModules = [
    "drbd"
    "drbd_transport_tcp"
    "loop"
  ];
  boot.extraModprobeConfig = ''
    options drbd usermode_helper=disabled
  '';

  environment.systemPackages = [ pkgs.drbd ];
  services.udev.packages = [ pkgs.drbd ];
  ducktape.k8sWorker.nodeLabels."storage.allegedly.works/drbd-canary" = "true";
}
