#!/bin/sh
set -eu

config_dir=/var/syncthing/config
data_dir=/var/syncthing/data

mkdir -p "$config_dir" "$data_dir" /sync-inbox
cp /identity/cert.pem "$config_dir/cert.pem"
cp /identity/key.pem "$config_dir/key.pem"
cp /syncthing-config/config.xml "$config_dir/config.xml"
chmod 0400 "$config_dir/cert.pem" "$config_dir/key.pem"
chmod 0600 "$config_dir/config.xml"

exec syncthing --config "$config_dir" --data "$data_dir" serve --no-browser --no-upgrade
