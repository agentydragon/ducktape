# Uncompressed NixOS rootfs tarball for the parked Runtime B worker image.
#
# The default `pixz -t` xz pass would be decompressed and re-gzipped by the
# former `podman import` publisher, and importing `.tar.xz` directly yields an
# inconsistent layer. Keep the tar uncompressed so a deliberate manual import
# can compress its layer once.
{ self }:
self.nixosConfigurations.haku-managed-agent.config.system.build.tarball.override {
  compressCommand = "cat";
  compressionExtension = "";
  extraInputs = [ ];
  # The agent toolset's `bash` tool execs `/bin/bash` at that literal path
  # (PATH-independent). NixOS activation would create it, but we run the closure
  # directly without booting, so bake /bin/{bash,sh} into the rootfs here.
  # extraCommands replaces the docker-container profile's value, so reapply
  # its /etc and /proc/sys/dev fixups too.
  extraCommands = self.nixosConfigurations.haku-managed-agent.pkgs.writeScript "haku-managed-agent-tarball-extra" ''
    rm etc
    mkdir -p proc sys dev etc bin
    chmod u+w bin
    ln -sf /sw/bin/bash bin/bash
    ln -sf /sw/bin/sh bin/sh
  '';
}
