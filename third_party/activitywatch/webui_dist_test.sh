#!/usr/bin/env bash
set -euo pipefail

dist="$1"
main_js=("$dist"/js/index.*.js)

[[ -f "$dist/index.html" ]]
[[ -f "$dist/manifest.json" ]]
[[ -f "$dist/sw.js" ]]
[[ ${#main_js[@]} -eq 1 ]]
grep -qF 'crossorigin="use-credentials"' "$dist/index.html"
grep -qF '.COMMIT_HASH="3cbe349"' "${main_js[0]}"
grep -qF '.getRegistrations()' "${main_js[0]}"
! grep -qF '.serviceWorker.register(' "${main_js[0]}"
grep -qF 'self.skipWaiting()' "$dist/sw.js"
grep -qF 'self.registration.unregister()' "$dist/sw.js"
grep -qE '\.navigate\([^)]*\.url\)' "$dist/sw.js"
! grep -qF 'importScripts' "$dist/sw.js"

# Every root-relative asset referenced by the entry document must exist in the
# bundle and use one of aw-server-rust's native webpack-era static routes.
while IFS= read -r url; do
  [[ -f "$dist$url" ]]
  case "$url" in
    /js/* | /css/* | /fonts/* | /static/* | /logo.png | /dark.css | /manifest.json) ;;
    *)
      echo "index.html references an asset aw-server-rust cannot serve: $url" >&2
      exit 1
      ;;
  esac
done < <(grep -oE '(src|href)="/[^"]+"' "$dist/index.html" | sed -E 's/^[^=]+="([^"]+)"$/\1/' | sort -u)
