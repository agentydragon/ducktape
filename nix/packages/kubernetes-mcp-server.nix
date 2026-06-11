# Kubernetes MCP Server — pre-built binary from GitHub releases.
# https://github.com/containers/kubernetes-mcp-server
{
  pkgs,
  lib,
}:
let
  version = "0.0.60";
in
pkgs.stdenv.mkDerivation {
  pname = "kubernetes-mcp-server";
  inherit version;
  src = pkgs.fetchurl {
    url = "https://github.com/containers/kubernetes-mcp-server/releases/download/v${version}/kubernetes-mcp-server-linux-amd64";
    hash = "sha256-UpUPjhtxzeG4POD5q/Rj1Ik1BJXm4Dq1Nmh4M3SUr3U=";
  };
  dontUnpack = true;
  installPhase = ''
    mkdir -p $out/bin
    cp $src $out/bin/kubernetes-mcp-server
    chmod +x $out/bin/kubernetes-mcp-server
  '';
  meta = {
    description = "Kubernetes MCP server (containers project)";
    homepage = "https://github.com/containers/kubernetes-mcp-server";
    license = lib.licenses.asl20;
    mainProgram = "kubernetes-mcp-server";
    platforms = [ "x86_64-linux" ];
  };
}
