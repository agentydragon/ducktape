# The stable OpenClaw gateway package, shared by the public-coder image
# (openclaw/) and Haku's spike image (haku/openclaw_spike/).
#
# Both images consume nix-openclaw's pinned npm-package gateway and source
# metadata, with this directory's release-specific dist patch. The source pin
# and patch are shared here rather than mirrored between image definitions.
#
# Gotcha: use the npm-package path. nix-openclaw's own stable uses it too; its
# from-source pnpm build is unexercised and lacks fetcherVersion-4 store steps
# (index.db reconstruction), which makes the gateway's offline install fail.
{
  pkgs,
  nix-openclaw,
}:

let
  ocPkgs = import nix-openclaw.inputs.nixpkgs {
    inherit (pkgs.stdenv.hostPlatform) system;
    overlays = [ nix-openclaw.overlays.default ];
  };

  # Keep the OpenClaw release, runtime-plugin version, source revision, and npm
  # dependency hash in lockstep with the nix-openclaw revision in flake.lock.
  # This image uses its npm-package path, so the source-build ownership patch
  # is unnecessary; the runtime plugins are bundled by the image consumer.
  sourceInfo = (import "${nix-openclaw}/nix/sources/openclaw-source.nix") // {
    applyNixStorePluginOwnershipPatch = false;
  };

  # Require the reviewed local patch to match nix-openclaw's OpenClaw release.
  distPatch = ./patches + "/openclaw-${sourceInfo.releaseVersion}-dist.patch";

  # Keep nix-openclaw's pinned npm wrapper and lockfile intact. Add only this
  # repo's release-specific dist patch, applying it fail-closed.
  patchedNixOpenclaw = ocPkgs.runCommand "nix-openclaw-openclaw-dist-patch" { } ''
    cp -r ${nix-openclaw} "$out"
    chmod -R u+w "$out"
    cp ${distPatch} "$out/nix/scripts/openclaw-npm-dist.patch"
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
    inherit sourceInfo;
  };
in
{
  inherit ocPkgs openclawPackages;

  # The pinned nix-openclaw installer now makes dist-runtime a symlink to dist,
  # so no downstream runtime-layout repair is needed here.
  gateway = openclawPackages.openclaw-gateway;
}
