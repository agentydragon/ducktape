set -euo pipefail

# openssh, coreutils, bash are on PATH from the image (/bin). No nix shell.
install -d -m 0700 /home/codex/.ssh
install -d -m 0755 /tmp/sshd /home/codex/.config/bazel
mkdir -p /workspace

# --- Forgejo git push identity ---
# Pushes to git.allegedly.works over SSH as the codex-pod Forgejo user (write
# collaborator on agentydragon/ducktape; tf/gitops/forgejo-agentydragon-repos),
# using the planted ~/.ssh/id_ed25519 whose pubkey is registered on that user.
cat >/home/codex/.ssh/config <<'SSHCFG'
Host git.allegedly.works
  HostName git.allegedly.works
  User git
  Port 2222
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
SSHCFG
chmod 0600 /home/codex/.ssh/config

# --- BuildBuddy (shared api key, reflected into this namespace as
# buildbuddy-api-key; optional — absent until reflected) ---
bb_key=/run/codex-creds/buildbuddy/api-key
if [ -r "$bb_key" ]; then
  cat >/home/codex/.config/bazel/buildbuddy.bazelrc <<BAZELRC
common --remote_header=x-buildbuddy-api-key=$(cat "$bb_key")
build --shell_executable=/bin/bash
BAZELRC
  chmod 0600 /home/codex/.config/bazel/buildbuddy.bazelrc
fi

# Login shells (ssh + kubectl exec) read creds live from the mounts, so nothing
# secret is baked into home. bbr/bb read BUILDBUDDY_API_KEY.
cat >/home/codex/.bashrc <<'BASHRC'
[ -r /run/codex-creds/buildbuddy/api-key ] &&
  export BUILDBUDDY_API_KEY="$(cat /run/codex-creds/buildbuddy/api-key)"
BASHRC

# --- Inbound sshd (kubectl-exec ProxyCommand access) ---
if [ ! -e /tmp/sshd/ssh_host_ed25519_key ]; then
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
