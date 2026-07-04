# Unofficial CLI for Z.AI capabilities: vision analysis, web search, web reader,
# and GitHub repo exploration. MCP-native. Not in nixpkgs.
# https://github.com/numman-ali/zai-cli
#
# Reads the API key from the environment (no key is baked in):
#   export Z_AI_API_KEY="..."      # (ZAI_API_KEY also accepted)
#   export Z_AI_MODE=ZHIPU          # use the Zhipu endpoint instead of z.ai
#
# UPDATING:
#   nix run nixpkgs#nix-update -- --flake zai-cli \
#     --version branch=main --url https://github.com/numman-ali/zai-cli
#   If npmDepsHash changes, build will fail with the correct hash — update manually.
{ pkgs, lib }:
pkgs.buildNpmPackage {
  pname = "zai-cli";
  version = "1.1.0";

  src = pkgs.fetchFromGitHub {
    owner = "numman-ali";
    repo = "zai-cli";
    rev = "v1.1.0"; # Latest as of 2026-07-03
    hash = "sha256-eEF5lwF5Aup17Z4fhjI0OLmNjbdS9ioRvCFtltzOcYY=";
  };

  # Monorepo: the package lives under packages/zai-cli (no workspace root).
  sourceRoot = "source/packages/zai-cli";

  npmDepsHash = "sha256-s0LXHJuRWpZx74G1ODgMoNMazoawJY6QKaugh/n3UwY=";

  buildPhase = ''
    runHook preBuild
    npm run build
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall

    mkdir -p $out/lib/zai-cli
    # bin/zai-cli.js imports ../dist/index.js (relative) — keep the layout.
    cp -r bin dist package.json node_modules $out/lib/zai-cli/

    mkdir -p $out/bin
    makeWrapper ${pkgs.nodejs}/bin/node $out/bin/zai-cli \
      --add-flags "$out/lib/zai-cli/bin/zai-cli.js"

    runHook postInstall
  '';

  nativeBuildInputs = [ pkgs.makeWrapper ];

  meta = {
    description = "Unofficial CLI for Z.AI capabilities: vision, web search, web reader, repo exploration";
    homepage = "https://github.com/numman-ali/zai-cli";
    license = lib.licenses.mit;
    mainProgram = "zai-cli";
  };
}
