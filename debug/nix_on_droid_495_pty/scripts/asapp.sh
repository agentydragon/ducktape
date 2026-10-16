#!/system/bin/sh
# Run the nix-on-droid proot payload as the app's own uid (u0_a199) AND in the
# app's own SELinux domain.
#
# Order matters: `su` resets the context to u:r:su:s0, so runcon has to run
# *inside* the su'd shell, not around it. Running as root instead of the app
# uid makes Nix reject $HOME ("cannot determine user's home directory") --
# getHome() ignores $HOME when the directory is not owned by the effective uid.
set -u
CTX=u:r:untrusted_app:s0:c512,c768
echo "-- identity --"
/system/bin/id
echo "-- ptytest inside proot --"
runcon $CTX /system/bin/sh /data/local/tmp/prootrun.sh "/android/mnt/payload/ptytest"
echo "-- local nix build inside proot --"
runcon $CTX /system/bin/sh /data/local/tmp/prootrun.sh "/bin/sh /tmp/nixbuild.sh 2>&1"
