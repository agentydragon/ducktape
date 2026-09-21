#!/bin/bash
# Repack the android-x86 initrd with the injected /scripts/5-custom boot hook.
set -euo pipefail
S=/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad
cd "$S/initrd"
find . | cpio -o -H newc --quiet | gzip -9 >"$S/vm/initrd-custom.img"
ls -la "$S/vm/initrd-custom.img"
