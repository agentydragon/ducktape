# Published by the existing Nix images workflow; Debian VM fixtures consume a
# digest-pinned filesystem with the native binary and its Nix runtime closure.
{ pkgs, pkgsUnstable }:
let
  codex = import ./package.nix { inherit pkgs pkgsUnstable; };
  root = pkgs.linkFarm "agentplane-codex-root" [
    {
      name = "opt/harnesses/codex/bin/codex";
      path = "${codex}/bin/codex";
    }
  ];
in
pkgs.dockerTools.buildLayeredImage {
  name = "git.allegedly.works/ducktape-ci/agentplane-codex";
  contents = [ root ];
  config = {
    Entrypoint = [ "/opt/harnesses/codex/bin/codex" ];
    Labels."works.allegedly.command-memory-protocol" = "1";
  };
}
