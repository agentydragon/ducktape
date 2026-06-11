# Gmail MCP Server package
#
# A Model Context Protocol server for Gmail integration.
# https://github.com/GongRzhe/Gmail-MCP-Server
#
# SETUP:
#   1. Create Google Cloud OAuth credentials:
#      - Go to https://console.cloud.google.com/apis/credentials
#      - Create OAuth 2.0 Client ID (Desktop app type)
#      - Download JSON and save as ~/.gmail-mcp/gcp-oauth.keys.json
#
#   2. Enable Gmail API:
#      - Go to https://console.cloud.google.com/apis/library/gmail.googleapis.com
#      - Click "Enable"
#
#   3. Run authentication:
#      $ gmail-mcp-auth
#      (Opens browser for Google OAuth consent)
#
#   4. Credentials stored in ~/.gmail-mcp/credentials.json
#
# UPDATING:
#   nix run nixpkgs#nix-update -- --flake gmail-mcp \
#     --version branch=main --url https://github.com/GongRzhe/Gmail-MCP-Server
#   If npmDepsHash changes, build will fail with correct hash - update manually.
{ pkgs, lib }:
let
  # Pinned commit - update this to upgrade
  # Latest as of 2026-02-05: a890d19189bbc1325b8728fab830fc278cfd8804
  rev = "a890d19189bbc1325b8728fab830fc278cfd8804";
  shortRev = builtins.substring 0 7 rev;
in
pkgs.buildNpmPackage {
  pname = "gmail-mcp-server";
  version = "1.1.11-git.${shortRev}";

  src = pkgs.fetchFromGitHub {
    owner = "GongRzhe";
    repo = "Gmail-MCP-Server";
    inherit rev;
    hash = "sha256-cmnnRwQUOro7idWQySzhUfkKcnnLcpVYsi8JwwHeypg=";
  };

  # Patch the broken package-lock.json that's missing resolved URL for @modelcontextprotocol/sdk
  postPatch = ''
    ${pkgs.jq}/bin/jq '
      .packages["node_modules/@modelcontextprotocol/sdk"] += {
        "resolved": "https://registry.npmjs.org/@modelcontextprotocol/sdk/-/sdk-0.4.0.tgz",
        "integrity": "sha512-79gx8xh4o9YzdbtqMukOe5WKzvEZpvBA1x8PAgJWL7J5k06+vJx8NK2kWzOazPgqnfDego7cNEO8tjai/nOPAA=="
      }
    ' package-lock.json > package-lock.json.tmp
    mv package-lock.json.tmp package-lock.json
  '';

  # Hash of npm dependencies (after patching the lockfile)
  npmDepsHash = "sha256-y4Hrjj9lAlMVJPcezK4SH0oZ8q9qseE9dkiVA1EtIec=";

  # Build TypeScript to JavaScript
  buildPhase = ''
    runHook preBuild
    npm run build
    runHook postBuild
  '';

  # Install the built package
  installPhase = ''
    runHook preInstall

    mkdir -p $out/lib/gmail-mcp-server
    cp -r dist package.json node_modules $out/lib/gmail-mcp-server/

    mkdir -p $out/bin

    # Main MCP server wrapper
    makeWrapper ${pkgs.nodejs}/bin/node $out/bin/gmail-mcp \
      --add-flags "$out/lib/gmail-mcp-server/dist/index.js"

    # Auth command wrapper (for initial setup)
    makeWrapper ${pkgs.nodejs}/bin/node $out/bin/gmail-mcp-auth \
      --add-flags "$out/lib/gmail-mcp-server/dist/index.js" \
      --add-flags "auth"

    runHook postInstall
  '';

  nativeBuildInputs = [ pkgs.makeWrapper ];

  meta = {
    description = "Gmail MCP server with auto authentication support";
    homepage = "https://github.com/GongRzhe/Gmail-MCP-Server";
    license = lib.licenses.isc;
    mainProgram = "gmail-mcp";
  };
}
