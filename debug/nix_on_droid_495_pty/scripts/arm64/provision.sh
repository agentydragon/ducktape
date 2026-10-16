#!/bin/bash
# Give the guest working, *trusted* internet through this container's egress
# proxy. Nothing here bypasses the proxy or weakens verification.
#
# Route: the emulator's user-mode network maps 10.0.2.2 to the host's loopback,
# which is where the agent proxy listens (127.0.0.1:39587). The guest dials the
# real proxy; no forwarder is involved.
#
# Trust: two stores, because two stacks make the calls.
#   - Android's own store serves the com.termux.nix APK's Java HTTP client,
#     which is what downloads the bootstrap zip. On Android 14 the live store
#     is the conscrypt APEX (/apex/com.android.conscrypt/cacerts), not
#     /system/etc/security/cacerts, so both are written; the APEX copy is a
#     tmpfs overlay, which has to be propagated into zygote's mount namespace
#     or already-running apps keep the old store.
#   - A plain PEM bundle serves nix-on-droid's curl and Nix inside the proot,
#     via NIX_SSL_CERT_FILE / SSL_CERT_FILE (set in run_nod.sh).
set -uo pipefail
D=/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad
ADB="$D/sdk/platform-tools/adb"
SER=emulator-5554
H=$(cat "$D/ca/hash.txt")
PROXY_HOST=10.0.2.2
PROXY_PORT=39587

a() { "$ADB" -s "$SER" "$@"; }

echo "=== device ==="
a shell 'getprop ro.build.version.release; getprop ro.product.cpu.abi; getenforce; getprop ro.build.type'

echo "=== root + writable system ==="
a root >/dev/null 2>&1
sleep 5
a wait-for-device
a remount 2>&1 | tail -2

echo "=== guest routing / reachability of the host proxy ==="
a shell "ip route; echo '--- tcp connect test ---'; echo | toybox nc -w 5 $PROXY_HOST $PROXY_PORT && echo PROXY_TCP_OK || echo PROXY_TCP_FAIL"

echo "=== push CA material ==="
a push "$D/ca/$H.0" /data/local/tmp/"$H".0 2>&1 | tail -1
a push "$D/ca/ca-bundle.crt" /data/local/tmp/ca-bundle.crt 2>&1 | tail -1

echo "=== install into /system store ==="
a shell "cp /data/local/tmp/$H.0 /system/etc/security/cacerts/$H.0 && chmod 644 /system/etc/security/cacerts/$H.0 && chown root:root /system/etc/security/cacerts/$H.0 && echo SYSTEM_STORE_OK"

echo "=== install into conscrypt APEX store (Android 14 live store) ==="
a shell "
set -e
APEXCA=/apex/com.android.conscrypt/cacerts
if [ ! -d \$APEXCA ]; then echo 'NO_APEX_CA_DIR'; exit 0; fi
rm -rf /data/local/tmp/ca-copy && mkdir -p /data/local/tmp/ca-copy
cp \$APEXCA/* /data/local/tmp/ca-copy/
cp /data/local/tmp/$H.0 /data/local/tmp/ca-copy/
chown root:root /data/local/tmp/ca-copy/*
chmod 644 /data/local/tmp/ca-copy/*
chcon u:object_r:system_file:s0 /data/local/tmp/ca-copy/*
mount -t tmpfs tmpfs \$APEXCA
cp /data/local/tmp/ca-copy/* \$APEXCA/
chown root:root \$APEXCA/*; chmod 644 \$APEXCA/*
chcon u:object_r:system_file:s0 \$APEXCA/*
echo APEX_STORE_OK: \$(ls \$APEXCA | wc -l) certs
"

echo "=== restart the framework so apps see the new store ==="
# zygote's children keep the mount namespace they were forked with, so the
# overlay only reaches apps after zygote re-forks. A framework restart is the
# portable way to force that (Android's toybox has no nsenter).
a shell 'stop && start' 2>&1 | tail -1
a wait-for-device
for _ in $(seq 1 60); do
  [ "$(a shell getprop sys.boot_completed 2>/dev/null | tr -d '\r\n')" = "1" ] && break
  sleep 15
done
a shell 'ls /apex/com.android.conscrypt/cacerts | wc -l'

echo "=== android global http proxy ==="
a shell "settings put global http_proxy $PROXY_HOST:$PROXY_PORT"
a shell "settings get global http_proxy"
