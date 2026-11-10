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
    font_dir="$out/share/fonts/truetype/bebas-neue"
    # The 2018 family only has Regular; 2014 includes the other weights.
    font_family='fonts/BebasNeue(2014)ByFontFabric'
    for weight in Bold Book Light Regular Thin; do
      test -f "$font_family/BebasNeue-$weight.ttf"
    done

    install_font() {
      font="$1"
      dest="$font_dir/''${font##*/}"
      if [ -e "$dest" ]; then
        echo "Duplicate font filename: $dest" >&2
        exit 1
      fi
      install -Dm644 "$font" "$dest"
    }

    while IFS= read -r -d "" font; do
      install_font "$font"
    done < <(find "$font_family" -type f -name '*.ttf' -print0)
    runHook postInstall
  '';

  meta = {
    description = "Bebas Neue sans-serif font family";
    homepage = "https://github.com/dharmatype/Bebas-Neue";
    license = lib.licenses.ofl;
    platforms = lib.platforms.all;
  };
}
