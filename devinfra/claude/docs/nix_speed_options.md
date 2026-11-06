# Historical Nix setup speed analysis

This note records an earlier attempt to shorten Claude Code web startup, when
Nix evaluation and fetching nixpkgs appeared to dominate setup. Its rough
~120-second cold-start and ~2-second direct-download (`binary_tools.py`)
figures were estimates, not controlled measurements or current timings. The
download workaround duplicated tool versions maintained by the flake.

The analysis considered four alternatives:

- Precompute store paths and `nix copy` them: skips evaluation but paths track
  nixpkgs revisions.
- Publish a store-closure tarball: avoids Nix at runtime, but adds CI/release
  work, architecture variants, and republishing on tool updates.
- Generate a direct-download catalog from the flake: reduces version drift but
  adds code generation and keeps two install mechanisms.
- Share versions in TOML: simple, but URLs and hashes need manual upkeep and
  the catalog is not Nix-native.

These were proposals, not implemented setup modes. Current web setup still
installs Nix and evaluates the selected flake output; it configures the Attic
binary cache so published closures can substitute. See the [current setup
guide](web_setup.md), [setup script](../web_setup.sh), [devtools package
owner](../../../nix/flake/devtools.nix), [Attic public bootstrap cache](../../../cluster/docs/nix_cache.md#public-bootstrap-cache),
and [CI cache targets](../../../devinfra/ci/nix_attic_targets.nix).
