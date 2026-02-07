#!/bin/bash
# 2. clear Pop-Shell workspace grabs
schema=org.gnome.shell.extensions.pop-shell
for k in switch-to-workspace-{left,right} move-to-workspace-{left,right}; do
  gsettings set "$schema" "$k" "@as []"
done
