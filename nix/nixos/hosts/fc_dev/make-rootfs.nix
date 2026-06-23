# Produces system.build.ext4 — an ext4 filesystem image containing the
# full NixOS system closure, suitable for Firecracker's virtio-blk rootfs.
{
  config,
  pkgs,
  lib,
  modulesPath,
  ...
}:
let
  make-ext4-fs = import (modulesPath + "/../lib/make-ext4-fs.nix");
in
{
  system.build.ext4 = make-ext4-fs {
    inherit pkgs lib;
    inherit (pkgs)
      e2fsprogs
      libfaketime
      perl
      fakeroot
      zstd
      ;
    storePaths = [ config.system.build.toplevel ];
    compressImage = false;
    volumeLabel = "fc-dev-rootfs";
    populateImageCommands = ''
      # System profile symlink — the only way to find the system closure.
      # process_api spawns /nix/var/nix/profiles/system/init to start systemd,
      # which then runs NixOS activation (creates /etc, /tmp, etc.).
      mkdir -p ./files/nix/var/nix/profiles
      ln -s ${config.system.build.toplevel} ./files/nix/var/nix/profiles/system

      # NixOS activation checks for this marker.
      mkdir -p ./files/etc
      touch ./files/etc/NIXOS
    '';
  };
}
