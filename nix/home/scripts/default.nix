# User utility scripts installed to ~/.local/bin via home.packages
#
# These are migrated from dotfiles/local/bin/
{
  config,
  pkgs,
  lib,
  ...
}:
let
  # Duplicate file finder (finds files with same md5sum in a directory)
  duplicity = pkgs.writeShellScriptBin "duplicity" ''
    if [ $# -lt 1 ]; then
      echo "Usage: $0 (directory)"
      exit
    fi

    soubory=""
    exmd5ka=""
    last=""
    first=false

    find $1 -type f -exec md5sum "{}" \; | sort | while read line; do
      name=$(echo $line | cut -d ' ' -f 2-)
      md5=$(echo $line | cut -d ' ' -f 1)
      if [ "$exmd5ka" == "$md5" ]; then
        if [ ! "$last" == "" ]; then
          if $first; then
            echo
          fi
          first=true
          echo -n "$last"
          last=""
        fi
        echo -n " $name"
      else
        last="$name"
      fi
      exmd5ka=$md5
    done

    echo
  '';

  # Theme switchers (set_light_theme, set_dark_theme) are in modules/solarized.nix
in
{
  home.packages = [
    duplicity
  ];
}
