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
    old_family='fonts/BebasNeue(2014)ByFontFabric'
    new_family='fonts/BebasNeue(2018)ByDhamraType/ttf'
    test -f "$new_family/BebasNeue-Regular.ttf"

    install_font() {
      font="$1"
      dest="$font_dir/''${font##*/}"
      if [ -e "$dest" ]; then
        echo "Duplicate font filename: $dest" >&2
        exit 1
      fi
      install -Dm644 "$font" "$dest"
    }

    # The 2018 family supplies Regular; retain the other 2014 weights.
    for weight in Bold Book Light Thin; do
      install_font "$old_family/BebasNeue-$weight.ttf"
    done
    while IFS= read -r -d "" font; do
      install_font "$font"
    done < <(find "$new_family" -type f -name '*.ttf' -print0)
    runHook postInstall
  '';

  meta = {
    description = "Bebas Neue sans-serif font family";
    homepage = "https://github.com/dharmatype/Bebas-Neue";
    license = lib.licenses.ofl;
    platforms = lib.platforms.all;
  };
}
