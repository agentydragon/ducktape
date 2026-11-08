# The Firecracker launcher mounts this OCI image at /images/boot. Keep the
# kernel, raw root filesystem, and init store path at the image root.
{
  pkgs,
  self,
}:
let
  guest = self.nixosConfigurations.agentplane-environment-firecracker;
  toplevel = guest.config.system.build.toplevel;
  closure = pkgs.closureInfo { rootPaths = [ toplevel ]; };
  rootFilesystem =
    pkgs.runCommand "agentplane-environment-firecracker-rootfs"
      {
        nativeBuildInputs = [
          pkgs.coreutils
          pkgs.e2fsprogs
          pkgs.fakeroot
        ];
      }
      ''
        mkdir -p "$out"
        ${pkgs.fakeroot}/bin/fakeroot -- bash -euo pipefail -c '
          rootfs="$TMPDIR/rootfs"
          mkdir -p "$rootfs/nix/store"
          while IFS= read -r store_path; do
            cp -a "$store_path" "$rootfs/nix/store/"
          done < ${closure}/store-paths
          mkdir -m 0755 -p \
            "$rootfs/etc" \
            "$rootfs/bin" \
            "$rootfs/sbin" \
            "$rootfs/dev" \
            "$rootfs/proc" \
            "$rootfs/sys" \
            "$rootfs/run" \
            "$rootfs/tmp" \
            "$rootfs/var" \
            "$rootfs/root" \
            "$rootfs/home" \
            "$rootfs/nix/var/nix/profiles"
          chmod 1777 "$rootfs/tmp"
          ln -s ${toplevel} "$rootfs/nix/var/nix/profiles/system"
          ln -s ${toplevel} "$rootfs/run/current-system"

          image_bytes=$(du -s --block-size=1 "$rootfs" | cut -f1)
          image_bytes=$((image_bytes + image_bytes / 5 + 1073741824))
          image_bytes=$(((image_bytes + 4095) / 4096 * 4096))
          blocks=$((image_bytes / 4096))
          truncate -s "$image_bytes" "$out/rootfs.ext4"
          E2FSPROGS_FAKE_TIME="$SOURCE_DATE_EPOCH" mkfs.ext4 \
            -F -m 0 -b 4096 -L agentplane-root \
            -U 00000000-0000-0000-0000-000000000001 \
            -E lazy_itable_init=0,lazy_journal_init=0,root_owner=0:0 \
            -d "$rootfs" "$out/rootfs.ext4" "$blocks"
          for path in etc bin sbin dev proc sys run var nix/var/nix/profiles; do
            debugfs -R "stat /$path" "$out/rootfs.ext4" 2>/dev/null | grep -F "Type: directory" >/dev/null
          done
          for path in run/current-system nix/var/nix/profiles/system; do
            debugfs -R "stat /$path" "$out/rootfs.ext4" 2>/dev/null | grep -F "Type: symlink" >/dev/null
          done
        '
      '';
  kernel = guest.config.boot.kernelPackages.kernel;
  bootFiles = pkgs.runCommand "agentplane-environment-firecracker-boot-files" { } ''
    mkdir -p "$out"
    # The kernel dev output's exact layout differs across kernel packages;
    # locate the installed ELF and fail loudly if there is not exactly one.
    find -L ${kernel.dev} -type f -name vmlinux -print > "$TMPDIR/vmlinux-candidates"
    test "$(wc -l < "$TMPDIR/vmlinux-candidates")" -eq 1
    install -m 0644 "$(cat "$TMPDIR/vmlinux-candidates")" "$out/vmlinux"
    cp ${rootFilesystem}/rootfs.ext4 "$out/rootfs.ext4"
    printf '%s\n' ${toplevel}/init > "$out/init"
  '';
in
pkgs.dockerTools.buildLayeredImage {
  name = "git.allegedly.works/ducktape-ci/agentplane-environment-firecracker";
  contents = [ bootFiles ];
  # buildLayeredImage uses symlinkJoin for contents. Dereference those links
  # into the custom layer: includeStorePaths=false intentionally omits the
  # Nix store layer, so the image volume must contain the actual boot files.
  extraCommands = ''
    for file in vmlinux rootfs.ext4 init; do
      cp --dereference -- "$file" "$file.real"
      mv -f -- "$file.real" "$file"
    done
  '';
  includeStorePaths = false;
  maxLayers = 2;
  config = { };
}
