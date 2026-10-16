#!/system/bin/sh
# Run a command inside nix-on-droid's proot, with exactly the binds and flags
# the generated bin/login uses (modules/environment/login/login.nix): no -r
# root, no fakeroot/-0, --link2symlink --sysvipc, and /dev untouched except
# /dev/shm -- so /dev/ptmx and /dev/pts inside are Android's real ones.
#
# Only the final command differs from bin/login, which would run
# usr/lib/login-inner (first-run setup, needs network).
set -u
U=/data/data/com.termux.nix/files/usr
export USER=nix-on-droid
export HOME=/data/data/com.termux.nix/files/home
export PROOT_TMP_DIR=$U/tmp
export PROOT_L2S_DIR=$U/.l2s

exec "$U/bin/proot-static" \
  -b "$U/nix:/nix" \
  -b "$U/bin:/bin" \
  -b "$U/etc:/etc" \
  -b "$U/tmp:/tmp" \
  -b "$U/usr:/usr" \
  -b "$U/dev/shm:/dev/shm" \
  -b /:/android \
  --link2symlink \
  --sysvipc \
  "$U/bin/sh" -c "$*"
