# The stable OpenClaw gateway package, shared by the public-coder image
# (openclaw/) and Haku's spike image (haku/openclaw_spike/).
#
# Both images consume nix-openclaw's npm-package gateway build, spliced with
# this directory's npm wrapper and release-specific dist patch. That splice,
# source pin, and patch are identical for both consumers, so they live here once
# rather than being mirrored between two image definitions and drifting apart.
#
# Gotcha: use the npm-package path, not a from-source `sourceInfo` override.
# nix-openclaw's own stable is npm-package too, so its from-source pnpm build is
# unexercised and is missing fetcherVersion-4 store steps (index.db
# reconstruction), which makes the gateway's offline install fail.
{
  pkgs,
  nix-openclaw,
}:

let
  # Keep the tested npm-package build path and explicitly align its wrapper and
  # source metadata with the 2026.9.4 release used by both images.
  ocPkgs = import nix-openclaw.inputs.nixpkgs {
    inherit (pkgs.stdenv.hostPlatform) system;
    overlays = [ nix-openclaw.overlays.default ];
  };

  # Mirrors nix/sources/openclaw-source.nix but pinned to 2026.9.4. Setting
  # `gatewayNpmDepsHash` (not `pnpmDepsHash`) selects the prebuilt-npm gateway
  # path -- the one stable uses. `runtimePluginVersion` tracks nix-openclaw's
  # generated acpx runtime plugin (2026.9.4), which requires the matching
  # OpenClaw host version.
  stableSourceInfo = {
    owner = "openclaw";
    repo = "openclaw";
    pnpmMajor = "12";
    applyPublicSurfaceHardlinksPatch = false;
    applySkipPluginAutoEnableNixModePatch = false;
    # 2026.9.4 changed the hardlink-policy source shape, so the old
    # nix-openclaw ownership patch no longer applies. Runtime plugins are
    # copied into the gateway's bundled extension tree by the consumer instead.
    applyNixStorePluginOwnershipPatch = false;
    releaseTag = "v2026.9.4";
    releaseVersion = "2026.9.4";
    runtimePluginVersion = "2026.9.4";
    # The npm path does not fetch the git source, but these mirror the stable
    # sourceInfo shape for checks and future source builds.
    rev = "3a9d69db306cd7f081e06254cb89c4bcc14a7107";
    hash = "sha256-xeUf0Emyhen4hnxjhbTI59d02QfB3YWTxhlqNkKuiUA=";
    # Filled from the Nix build's fixed-output error after the wrapper lock is
    # regenerated.
    gatewayNpmDepsHash = "sha256-L6Y69xvZFrwG3Hb2iKG2FLraQqLtxtupImFR4KLTs9Q=";
  };

  # nix-openclaw's npm wrapper (nix/npm/openclaw/) pins openclaw to an older
  # stable release, and openclaw-gateway-npm.nix asserts the lock version equals
  # `sourceInfo.releaseVersion`. Splice this directory's wrapper over it.
  #
  # Regenerate npm_wrapper/ with:
  #   npm install openclaw@<ver> --package-lock-only --omit=dev --install-strategy=nested
  # `--install-strategy=nested` is load-bearing: this release ships no
  # npm-shrinkwrap.json, so a default (hoisted) install lifts all of openclaw's
  # runtime deps to the wrapper's top-level node_modules -- but nix-openclaw's
  # install script copies only node_modules/openclaw/., so the gateway would ship
  # with ZERO runtime deps and crash at its first import (tslog / undici).
  # Nesting mirrors what stable's shrinkwrap does, so the deps sit under
  # node_modules/openclaw and get copied into the gateway.
  patchedNixOpenclaw = ocPkgs.runCommand "nix-openclaw-openclaw-stable-wrapper" { } ''
    cp -r ${nix-openclaw} "$out"
    chmod -R u+w "$out"
    cp ${./npm_wrapper/package.json} "$out/nix/npm/openclaw/package.json"
    cp ${./npm_wrapper/package-lock.json} "$out/nix/npm/openclaw/package-lock.json"
    cp ${./patches/openclaw-2026.9.4-dist.patch} "$out/nix/scripts/openclaw-npm-dist.patch"
    substituteInPlace "$out/nix/packages/openclaw-gateway-npm.nix" \
      --replace-fail 'patch-openclaw-npm-dist.mjs' 'openclaw-npm-dist.patch'
    # Apply the release-specific repairs as a conventional, fail-closed patch.
    # Keep the upstream installer contract's path variable: it is validated
    # before use, and the patch command itself is pinned into the build.
    substituteInPlace "$out/nix/scripts/openclaw-gateway-npm-install.sh" \
      --replace-fail \
        'OPENCLAW_PACKAGE_ROOT="$root" "$NODE_BIN" "$OPENCLAW_PATCH_NPM_DIST_SCRIPT"' \
        '${ocPkgs.patch}/bin/patch --batch --fuzz=0 --directory="$root" --strip=1 --input="$OPENCLAW_PATCH_NPM_DIST_SCRIPT"'
  '';

  openclawPackages = import "${patchedNixOpenclaw}/nix/packages" {
    pkgs = ocPkgs;
    sourceInfo = stableSourceInfo;
  };
in
{
  inherit ocPkgs openclawPackages;

  # The pinned nix-openclaw installer now makes dist-runtime a symlink to dist,
  # so no downstream runtime-layout repair is needed here.
  gateway = openclawPackages.openclaw-gateway;
}
