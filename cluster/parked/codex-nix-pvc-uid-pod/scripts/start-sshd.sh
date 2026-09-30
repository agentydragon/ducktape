set -euo pipefail

for dir in /tmp/bin /tmp/sshd /tmp/xdg-cache /home/codex/.cache /home/codex/.config/direnv /home/codex/.config/nix; do
  install -d -m 0755 "$dir"
done
mkdir -p /workspace

cat >/home/codex/.config/nix/nix.conf <<'EOF'
experimental-features = nix-command flakes
accept-flake-config = true
EOF

session_path="/nix/var/nix/profiles/default/bin:$PATH"

ln -sf "$(command -v busybox)" /tmp/bin/nc
if [ ! -e /tmp/sshd/ssh_host_ed25519_key ]; then
  ssh-keygen -q -t ed25519 -N "" -f /tmp/sshd/ssh_host_ed25519_key
fi

cat >/tmp/sshd/sshd_config <<'EOF'
Port 2222
ListenAddress 127.0.0.1
HostKey /tmp/sshd/ssh_host_ed25519_key
AuthorizedKeysFile /etc/ssh/authorized_keys/codex
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
AllowUsers codex
StrictModes no
UsePAM no
PermitTTY yes
X11Forwarding no
AllowTcpForwarding no
Subsystem sftp internal-sftp
PidFile /tmp/sshd/sshd.pid
LogLevel VERBOSE
EOF

printf 'SetEnv PATH=%s HOME=/home/codex USER=codex\n' "$session_path" >>/tmp/sshd/sshd_config
exec "$(command -v sshd)" -D -e -f /tmp/sshd/sshd_config
