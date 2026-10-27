# Host kernel support for a Piraeus (LINSTOR) satellite. LINSTOR owns every DRBD
# resource, so this deliberately does not enable `services.drbd` (a static
# drbd.conf brought up by `drbdadm up all`).
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.ducktape.piraeusHost;
in
{
  options.ducktape.piraeusHost = {
    enable = lib.mkEnableOption "DRBD 9 kernel support for a Piraeus satellite";
    drbdPackage = lib.mkOption {
      type = lib.types.package;
      default = config.boot.kernelPackages.drbd;
      defaultText = lib.literalExpression "config.boot.kernelPackages.drbd";
      description = "Out-of-tree DRBD 9 kernel module, built for the host's kernel.";
    };
  };

  config = lib.mkIf cfg.enable {
    # The kernel's in-tree drbd is 8.4, which LINSTOR cannot drive; the
    # out-of-tree module shadows it. Modules load from the booted system, so a
    # newly enabled host needs a reboot before `modinfo drbd` reports 9.x.
    boot.extraModulePackages = [ cfg.drbdPackage ];
    boot.kernelModules = [
      "drbd"
      "drbd_transport_tcp"
      "dm_thin_pool"
    ];
    # LINSTOR reacts to DRBD state changes itself; Piraeus requires the kernel's
    # usermode helper to be off.
    boot.extraModprobeConfig = ''
      options drbd usermode_helper=disabled
    '';
    services.udev.packages = [ pkgs.drbd ];
  };
}
