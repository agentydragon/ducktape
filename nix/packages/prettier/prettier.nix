# Prettier bundled with plugins (svelte) so .prettierrc.cjs's require() resolves.
#
# Single source of truth for prettier version — used by devshell, pre-commit
# (language: system), GHA (setup-nix-devtools), and update_image_pin.py.
#
# UPDATING:
#   1. Bump versions in package.json
#   2. Regenerate lockfile: cd nix/packages/prettier && npm install --package-lock-only
#   3. Build will fail with correct npmDepsHash — update it below
{ pkgs }:
pkgs.buildNpmPackage {
  pname = "prettier-with-plugins";
  version = "3.8.1";

  src = ./.;

  npmDepsHash = "sha256-Qw5eA5gTdFv4v0jIuGyWuT/JRjojzKJJArQKs0Po8AQ=";

  dontBuild = true;

  installPhase = ''
    runHook preInstall

    mkdir -p $out/lib/prettier
    cp -r node_modules $out/lib/prettier/

    mkdir -p $out/bin
    makeWrapper ${pkgs.nodejs}/bin/node $out/bin/prettier \
      --add-flags "$out/lib/prettier/node_modules/prettier/bin/prettier.cjs" \
      --set NODE_PATH "$out/lib/prettier/node_modules"

    runHook postInstall
  '';

  nativeBuildInputs = [ pkgs.makeWrapper ];

  meta = {
    description = "Prettier with svelte plugin";
    homepage = "https://prettier.io";
    mainProgram = "prettier";
  };
}
