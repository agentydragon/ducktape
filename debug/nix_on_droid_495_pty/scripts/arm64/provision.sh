#!/bin/bash
# Make the guest able to reach the internet through this container's egress
# proxy, and trust it.
#
# Network: the emulator's user-mode stack maps 10.0.2.2 to the host's loopback,
# which is where the agent proxy listens (127.0.0.1:39587). Nothing is
# forwarded or disabled -- the guest dials the real proxy.
#
# Trust: two stores, because two stacks make the calls. Android's system store
# (<subject_hash_old>.0) serves the APK's Java HTTP client; a plain PEM bundle
# serves nix-on-droid's curl/Nix inside the proot.
set -uo pipefail
D=/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad
ADB="$D/sdk/platform-tools/adb"
SER=emulator-5554
H=$(cat "$D/ca/hash.txt")
PROXY=10.0.2.2:39587

a() { "$ADB" -s "$SER" "$@"; }

echo "=== device ==="
a shell getprop ro.build.version.release
a shell getprop ro.product.cpu.abi
a shell getenforce

echo "=== root + writable system ==="
a root >/dev/null 2>&1
sleep 3
a remount 2>&1 | tail -2

echo "=== system cacerts store ==="
a push "$D/ca/$H.0" /data/local/tmp/"$H".0 2>&1 | tail -1
a shell "cp /data/local/tmp/$H.0 /system/etc/security/cacerts/$H.0 && chmod 644 /system/etc/security/cacerts/$H.0 && chown root:root /system/etc/security/cacerts/$H.0 && echo SYSTEM_STORE_OK" 2>&1 | tail -2
a shell "ls /system/etc/security/cacerts/ | wc -l"

echo "=== conscrypt APEX store (Android 14 runtime trust source) ==="
a shell 'ls /apex/com.android.conscrypt/cacerts/ 2>/dev/null | wc -l'

echo "=== proxy bundle for nix/curl ==="
a push "$D/ca/ca-bundle.crt" /data/local/tmp/ca-bundle.crt 2>&1 | tail -1

echo "=== android global http proxy ==="
a shell "settings put global http_proxy $PROXY"
a shell "settings get global http_proxy"
