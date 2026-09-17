set -euo pipefail

if [ ! -e /nix-pvc/var/nix/db/db.sqlite ]; then
  rm -rf /nix-pvc/.seed-tmp
  mkdir -p /nix-pvc/.seed-tmp
  tar -C /nix -cpf - . | tar -C /nix-pvc/.seed-tmp -xpf -
  shopt -s dotglob nullglob
  mv /nix-pvc/.seed-tmp/* /nix-pvc/
  rmdir /nix-pvc/.seed-tmp
fi

if [ "$(stat -c %u:%g /nix-pvc)" != 1000:1000 ]; then
  chown -R 1000:1000 /nix-pvc
fi

install -d -m 0700 -o 1000 -g 1000 /home-codex/.ssh
install -m 0600 -o 1000 -g 1000 /run/codex-bootstrap/id_ed25519 /home-codex/.ssh/id_ed25519
chown 1000:1000 /home-codex
