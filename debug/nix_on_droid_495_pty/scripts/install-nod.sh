#!/system/bin/sh
# Install the nix-on-droid bootstrap by hand, the way the com.termux.nix app
# would, straight into the app's data directory.
#
# The app itself is only a downloader plus a terminal: it unzips this exact
# zipball into /data/data/com.termux.nix/files/usr, replays SYMLINKS.txt and
# EXECUTABLES.txt (a zip written by Android's own zip writer carries neither
# symlinks nor the exec bit), and then runs bin/login. Doing it here avoids
# `pm install`, which repeatedly killed system_server under TCG.
set -u

PKG=/data/data/com.termux.nix
USR=$PKG/files/usr
APPUID=10199
APPCTX=u:object_r:app_data_file:s0:c512,c768

echo "=== creating $USR ==="
rm -rf "$PKG"
mkdir -p "$USR" "$PKG/files/home" || exit 1

echo "=== unzipping bootstrap (4072 files, this is slow under TCG) ==="
/system/xbin/unzip -q -o /mnt/payload/bootstrap-x86_64.zip -d "$USR" || exit 1

echo "=== replaying SYMLINKS.txt ==="
# Each line is "<target>\xe2\x86\x90<linkpath>" (U+2190 LEFTWARDS ARROW).
n=0
while IFS= read -r line; do
  target=${line%←*}
  link=${line#*←}
  [ -z "$target" ] && continue
  mkdir -p "$USR/${link%/*}" 2>/dev/null
  ln -s "$target" "$USR/$link" 2>/dev/null && n=$((n + 1))
done <"$USR/SYMLINKS.txt"
echo "  created $n symlinks"

echo "=== replaying EXECUTABLES.txt ==="
n=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  chmod 0755 "$USR/$f" 2>/dev/null && n=$((n + 1))
done <"$USR/EXECUTABLES.txt"
echo "  chmod +x on $n files"

echo "=== labelling as an app data dir (uid $APPUID, $APPCTX) ==="
chown -R $APPUID:$APPUID "$PKG"
chcon -R "$APPCTX" "$PKG" 2>/dev/null
ls -ldZ "$USR" "$USR/bin/proot-static" "$USR/bin/login"
echo "=== done ==="
