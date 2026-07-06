#!/bin/sh
set -eu

config_dir=/var/syncthing/config
data_dir=/var/syncthing/data
folder_dir=/sync-inbox/rugged
rugged_device_id=PATWINW-6VZGFXN-GFP24UN-CEAF4TB-YDBFV25-WDYEFC7-672W5RB-OQGGNQT

mkdir -p "$config_dir" "$data_dir" "$folder_dir"
cp /identity/cert.pem "$config_dir/cert.pem"
cp /identity/key.pem "$config_dir/key.pem"
chmod 0400 "$config_dir/cert.pem" "$config_dir/key.pem"

if [ ! -f "$config_dir/config.xml" ]; then
  syncthing --config "$config_dir" --data "$data_dir" generate --no-port-probing
fi

syncthing --config "$config_dir" --data "$data_dir" serve --no-browser --no-upgrade &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true' INT TERM EXIT

for _ in $(seq 1 120); do
  if syncthing --config "$config_dir" --data "$data_dir" cli config devices list >/tmp/syncthing-devices 2>/tmp/syncthing-cli.err; then
    break
  fi
  sleep 1
done

if [ ! -s /tmp/syncthing-devices ]; then
  echo "Syncthing CLI did not become ready" >&2
  cat /tmp/syncthing-cli.err >&2 || true
  exit 1
fi

if ! grep -qx "$rugged_device_id" /tmp/syncthing-devices; then
  syncthing --config "$config_dir" --data "$data_dir" cli config devices add-json \
    "{\"deviceID\":\"$rugged_device_id\",\"name\":\"rugged\",\"addresses\":[\"dynamic\"],\"compression\":\"metadata\"}"
fi

syncthing --config "$config_dir" --data "$data_dir" cli config folders list >/tmp/syncthing-folders
if ! grep -qx "activitywatch-rugged" /tmp/syncthing-folders; then
  syncthing --config "$config_dir" --data "$data_dir" cli config folders add-json \
    "{\"id\":\"activitywatch-rugged\",\"label\":\"ActivityWatch rugged\",\"path\":\"$folder_dir\",\"type\":\"receiveonly\",\"devices\":[{\"deviceID\":\"$rugged_device_id\"}],\"rescanIntervalS\":60,\"fsWatcherEnabled\":true}"
fi

wait "$pid"
