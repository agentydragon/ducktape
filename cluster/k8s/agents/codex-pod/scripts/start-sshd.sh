set -euo pipefail

# openssh, coreutils, bash are on PATH from the image (/bin). No nix shell.
install -d -m 0700 /home/codex/.ssh
install -d -m 0755 /tmp/sshd
mkdir -p /workspace

if [ \! -e /tmp/sshd/ssh_host_ed25519_key ]; then
  ssh-keygen -q -t ed25519 -N "" -f /tmp/sshd/ssh_host_ed25519_key
fi

cat >/tmp/sshd/sshd_config <<'SSHD'
Port 2222
ListenAddress 127.0.0.1
HostKey /tmp/sshd/ssh_host_ed25519_key
AuthorizedKeysFile /etc/ssh/authorized_keys/codex
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
AllowUsers codex
UsePAM no
StrictModes no
PermitTTY yes
Subsystem sftp internal-sftp
PidFile /tmp/sshd/sshd.pid
LogLevel VERBOSE
SetEnv PATH=/bin HOME=/home/codex USER=codex
SSHD

exec sshd -D -e -f /tmp/sshd/sshd_config
