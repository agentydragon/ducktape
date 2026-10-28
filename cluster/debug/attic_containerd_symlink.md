# Verify the NixOS worker rollout for the Attic symlink fix

**Source fix:** Completed in repository history. The default `nixpkgs` input is
now NixOS 26.05, and commit `d422ca9387` removed the containerd overlay after
upgrading the distro package to 2.3.0. The root cause and source-level fix are
recorded in
[`cluster/docs/lessons_learned/2026_03_23_containerd_nixos_absolute_passwd_symlink.md`](../docs/lessons_learned/2026_03_23_containerd_nixos_absolute_passwd_symlink.md).

**Still unverified:** The repository does not record the live containerd version
on `rugged`, `wyrm2`, or `iguana`. Before closing this follow-up, check each
worker's runtime version and confirm the Attic Pod can be created with its
configured `runAsUser`. The old host-version table in this note was from
2026-05-15 and is superseded by the source update.
