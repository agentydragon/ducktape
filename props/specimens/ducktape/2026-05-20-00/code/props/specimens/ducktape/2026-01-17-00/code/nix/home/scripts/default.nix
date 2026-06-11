# User utility scripts installed to ~/.local/bin via home.packages
#
# These are migrated from dotfiles/local/bin/
{
  config,
  pkgs,
  lib,
  ...
}: let
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

  # Purge a file from git history (uses filter-branch)
  git-purge-file = pkgs.writeShellScriptBin "git-purge-file" ''
    # TODO: usage; spaces in name!
    COMMAND="git rm --cached --ignore-unmatch '$1'"
    git filter-branch -f --index-filter "$COMMAND" --prune-empty --tag-name-filter cat -- --all
  '';

  # Theme switchers (depend on switch_gnome_terminal_profile - may be obsolete)
  set_dark_theme = pkgs.writeShellScriptBin "set_dark_theme" ''
    switch_gnome_terminal_profile --profile='Solarized Dark'
  '';

  set_light_theme = pkgs.writeShellScriptBin "set_light_theme" ''
    switch_gnome_terminal_profile --profile='Solarized Light'
  '';
in {
  home.packages = [
    duplicity
    git-purge-file
    set_dark_theme
    set_light_theme
  ];
}
