#!/usr/bin/env bash
# Serve the built website image (`result`, a docker archive) and fetch every top-level page.
set -euo pipefail
image="$(docker load -i "$(readlink -f result)" | sed -n 's/^Loaded image: //p')"
docker run -d --rm --name website -p 8080:8080 "$image"
for path in / /about.html /found.html /nfc.html /atom.xml /rss.xml; do
  curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 1 \
    -o /dev/null "http://127.0.0.1:8080$path"
done
docker stop website
