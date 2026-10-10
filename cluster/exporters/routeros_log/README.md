# RouterOS log shipper

Ships a MikroTik RouterOS device's log to Loki by polling `/log/print` over api-ssl, with each
line's own timestamp. The device keeps its log in a memory ring, so a poll after any gap (a WAN
outage, a restart of this shipper) backfills what the ring still holds. Deployed for the home
switch from `cluster/cdk8s/monitoring/`.

Needs a RouterOS user with the `api` and `read` policies, and the CA that signed the device's
api-ssl certificate. `/metrics` reports lines pushed and poll outcomes.

```bash
ROUTEROS_LOG_HOST=192.168.1.100 ROUTEROS_LOG_USERNAME=monitoring ROUTEROS_LOG_PASSWORD=... \
  ROUTEROS_LOG_CA_FILE=ca.crt ROUTEROS_LOG_LOKI_PUSH_URL=http://localhost:3100/loki/api/v1/push \
  ROUTEROS_LOG_LABELS='{"job": "home-switch"}' \
  bazel run //cluster/exporters/routeros_log:main_image_bin
```

Settings: <settings.py>.
