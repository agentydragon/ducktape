# The stable OpenClaw gateway package, shared by the public-coder image
# (openclaw/) and Haku's spike image (haku/openclaw_spike/).
#
# Both images consume nix-openclaw's npm-package gateway build, spliced with
# this directory's npm wrapper. That splice, the source pin, and the dist
# repairs below are identical for both consumers, so they live here once rather
# than being mirrored between two image definitions and drifting apart.
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
  # nix-openclaw's own source pin still tracks an older stable release. Keep its
  # tested npm-package build path, but splice the current stable wrapper and
  # source metadata over it so the images actually contain 2026.8.1.
  ocPkgs = import nix-openclaw.inputs.nixpkgs {
    inherit (pkgs.stdenv.hostPlatform) system;
    overlays = [ nix-openclaw.overlays.default ];
  };

  # Mirrors nix/sources/openclaw-source.nix but pinned to 2026.8.1. Setting
  # `gatewayNpmDepsHash` (not `pnpmDepsHash`) selects the prebuilt-npm gateway
  # path -- the one stable uses. `runtimePluginVersion` tracks nix-openclaw's
  # generated acpx runtime plugin (2026.7.1), not the gateway version; acpx
  # 2026.7.1 declares openclawCompat >=2026.7.1, so it is compatible with the
  # stable host.
  stableSourceInfo = {
    owner = "openclaw";
    repo = "openclaw";
    pnpmMajor = "12";
    applyPublicSurfaceHardlinksPatch = false;
    applySkipPluginAutoEnableNixModePatch = false;
    # 2026.8.1 changed the hardlink-policy source shape, so the old
    # nix-openclaw ownership patch no longer applies. Runtime plugins are
    # copied into the gateway's bundled extension tree by the consumer instead.
    applyNixStorePluginOwnershipPatch = false;
    releaseTag = "v2026.8.1";
    releaseVersion = "2026.8.1";
    runtimePluginVersion = "2026.7.1";
    # The npm path does not fetch the git source, but these mirror the stable
    # sourceInfo shape for checks and future source builds.
    rev = "ea806575e6450e4d1efdfc72c19f04be982a1b9b";
    hash = "sha256-9mYcHVti8iV47jByNLIMTXevyamNP82ZHQldzwbt8pg=";
    # Filled from the Nix build's fixed-output error after the wrapper lock is
    # regenerated.
    gatewayNpmDepsHash = "sha256-KnAPTULugA20oTb0Mkh82CajOBBC+LBg+Zx5nugwpAk=";
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
    cp ${./patch-openclaw-npm-dist.mjs} "$out/nix/scripts/patch-openclaw-npm-dist.mjs"
    # 2026.8.1 rejects an ACPX package root that is a symlink outside the
    # bundled extension tree. Copy the generated plugin into the dist instead.
    substituteInPlace "$out/nix/scripts/openclaw-gateway-npm-install.sh" \
      --replace-fail 'ln -s "$OPENCLAW_BUNDLED_ACPX" "$acpx_root"' \
      'cp -R "$OPENCLAW_BUNDLED_ACPX/." "$acpx_root"'
  '';

  openclawPackages = import "${patchedNixOpenclaw}/nix/packages" {
    pkgs = ocPkgs;
    sourceInfo = stableSourceInfo;
  };
in
{
  inherit ocPkgs openclawPackages;

  # nix-openclaw's stage_dist_runtime copies dist/extensions into dist-runtime/
  # and nothing else, but the extension modules import shared chunks as
  # ../../<chunk>.js -- resolving to dist/ in the upstream layout and to
  # dist-runtime/ here, where only extensions/ exists. 496 of 526 extension files
  # import such a chunk. OpenClaw prefers dist-runtime/extensions over
  # dist/extensions when present, so the partial tree is worse than none:
  # workboard's doctor contract is loaded by a legacy state migration, and its
  # ERR_MODULE_NOT_FOUND becomes a blocking startup-migration warning that
  # refuses to report the gateway ready.
  #
  # Repaired on the built output rather than by rewriting their install script,
  # so this does not depend on the exact shell line surviving upstream edits.
  # Links are relative because both trees are copied into $out together.
  #
  # Appended to installPhase, not a postInstall hook: nix-openclaw supplies a
  # complete custom installPhase and never calls `runHook postInstall`, so a
  # postInstall here is silently skipped and the guard below never runs.
  gateway = openclawPackages.openclaw-gateway.overrideAttrs (previous: {
    installPhase =
      previous.installPhase
      + "\n"
      + ''
        for runtime in "$out"/lib/*/dist-runtime; do
          dist="$(dirname "$runtime")/dist"
          if [ ! -d "$runtime" ] || [ ! -d "$dist" ]; then
            continue
          fi

          for entry in "$dist"/*; do
            name="$(basename "$entry")"
            if [ "$name" = extensions ] || [ -e "$runtime/$name" ]; then
              continue
            fi
            ln -s "../dist/$name" "$runtime/$name"
          done

          # Fail closed. Resolve each specifier against its own importer, because a
          # nested extension file's ../../ means extensions/, not the tree root; scan
          # .js only, since .d.ts references are type-level; and match bare specifier
          # strings rather than `from "..."`, because workboard's is a dynamic
          # import() -- the exact one that took the gateway down.
          #
          # Skip nested node_modules: stage_acpx splices in a plugin carrying its own
          # vendored packages, which resolve through their own tree, not through the
          # shared chunks this staging is responsible for.
          missing="$(grep -rHoE --include='*.js' --exclude-dir=node_modules '"(\.\./)+[A-Za-z0-9_.-]+\.js"' "$runtime/extensions" \
            | sed -E 's/:"/\t/; s/"$//' | sort -u \
            | while IFS="$(printf '\t')" read -r file spec; do
                if [ ! -e "$(dirname "$file")/$spec" ]; then
                  printf '%s -> %s\n' "$file" "$spec"
                fi
              done)"
          if [ -n "$missing" ]; then
            echo "dist-runtime is missing chunks its extensions import:" >&2
            echo "$missing" >&2
            exit 1
          fi
        done
      '';
  });
}
