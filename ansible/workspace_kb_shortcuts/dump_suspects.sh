#!/bin/bash

bk="shortcuts-$(date +%F-%H%M%S).dconf"
for p in \
  /org/gnome/desktop/wm/keybindings/ \
  /org/gnome/shell/keybindings/ \
  /org/gnome/shell/extensions/pop-shell/; do
  {
    echo "# $p"
    dconf dump "$p"
    echo
  } >>"$bk"
done
echo "backup → ./$bk"
