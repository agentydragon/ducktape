#!/bin/sh
# Start production Stalwart without provisioning. Mounted from the bootstrap-owned configuration ConfigMap.
# cap_net_bind_service, which cannot execute under no-new-privileges. This
# deployment uses unprivileged ports, so execute a copy without the xattr.
set -eu

RUN=/tmp/stalwart-run
mkdir -p "$RUN"
cp /usr/local/bin/stalwart "$RUN/stalwart"
exec "$RUN/stalwart" --config /etc/stalwart/config.json
