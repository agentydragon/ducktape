# The homepage as an OCI image: Hakyll renders the site at build time and
# static-web-server serves the result. Hakyll comes from nixpkgs' binary cache,
# so the build compiles only site.hs.
#
# Build: nix build .#website-image
# Load:  docker load < result
{ pkgs }:
let
  generator = pkgs.runCommand "website-generator" {
    nativeBuildInputs = [ (pkgs.haskellPackages.ghcWithPackages (p: [ p.hakyll ])) ];
  } "mkdir -p $out/bin && ghc -O1 -outputdir . -o $out/bin/site ${./site.hs}";

  site =
    pkgs.runCommand "website-site"
      {
        src = pkgs.lib.fileset.toSource {
          root = ./.;
          fileset = pkgs.lib.fileset.unions [
            ./CNAME
            ./about.markdown
            ./css
            ./found.markdown
            ./images
            ./index.html
            ./nfc.markdown
            ./posts
            ./static
            ./templates
          ];
        };
        LANG = "C.UTF-8";
      }
      ''
        cp -r $src src && chmod -R u+w src && cd src
        ${generator}/bin/site build
        mv _site $out
      '';
in
pkgs.dockerTools.buildLayeredImage {
  name = "website";
  tag = "latest";
  config = {
    User = "65534:65534";
    ExposedPorts."8080/tcp" = { };
    Entrypoint = [
      "${pkgs.static-web-server}/bin/static-web-server"
      # Its default host is [::], which fails to bind where the pod has no IPv6.
      "--host=0.0.0.0"
      "--port=8080"
      "--root=${site}"
      "--health=true"
    ];
    Labels."org.opencontainers.image.source" = "https://github.com/agentydragon/ducktape";
  };
}
