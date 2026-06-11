# Bebas Neue — free sans-serif font family
{ lib, pkgs }:
pkgs.stdenvNoCC.mkDerivation {
  pname = "bebas-neue-font";
  version = "2.000";

  src = pkgs.fetchFromGitHub {
    owner = "dharmatype";
    repo = "Bebas-Neue";
    rev = "master";
    hash = "sha256-dtllW1h2yxklwh7dztWeMbIfBGWllMt15S1fZO1YJUs=";
  };

  installPhase = ''
    runHook preInstall
    find . -name '*.ttf' -exec install -Dm644 {} -t $out/share/fonts/truetype/bebas-neue \;
    runHook postInstall
  '';

  meta = {
    description = "Bebas Neue sans-serif font family";
    homepage = "https://github.com/dharmatype/Bebas-Neue";
    license = lib.licenses.ofl;
    platforms = lib.platforms.all;
  };
}
