## Per-host setup for deployment

Grab `VAULT_KEY`. Save in keyring:

```bash
echo -n "$VAULT_KEY" | \
  secret-tool store --label='ansible-vault ducktape' \
    service ansible-vault account ducktape
```

## Generating secrets

To generate and encrypt a secret in one go:

```bash
# Generate a 32-character password and encrypt it
python3 -c "import secrets; print(secrets.token_urlsafe(32))" | \
  ansible-vault encrypt_string --stdin-name 'vault_variable_name'
```

## To update requirements

```bash
cd ansible
ansible-galaxy role install -r requirements.yaml
ansible-galaxy collection install -r requirements.yaml
```

These install into the default `~/.ansible/{roles,collections}` paths so
third-party content never lands in `ansible/roles` or `ansible/collections`.

## To deploy

```bash
cd ansible
ansible-playbook agentydragon.yaml --ask-become-pass
ansible-playbook vps.yaml
```

NOTE: running with `--skip-tags` might not work in any reasonable way. I didn't
assign task particularly with that in mind... :/

## To deploy gpd

```bash
cd ansible
ansible-playbook gpd.yaml --ask-become-pass
```

## Manual laptop installation steps

These parts can't be done by Ansible:

- `ssh-keygen`
- Add key to GitHub/GitLab
- `apt install git ansible`
- `git clone git@gitlab.com:agentydragon/ducktape`
- `ansible-playbook agentydragon.yaml --ask-become-pass`
- Add `~/.config/bazelrc.secrets` - see the `bazelrc` dotfile. Global `bazelrc` imports this file, it's supposed to contain the path (and
  password) to the Bazel cache on the VPS.

## Manual VM/Remote Machine Setup

When provisioning a new VM or remote machine:

1. The ducktape repository must be cloned before running the playbook.
2. Generate SSH key and add to GitHub/GitLab:

   ```bash
   # On the new machine, generate SSH key:
   ssh-keygen -t ed25519 -C "agentydragon@HOSTNAME"

   # Add both GitHub and GitLab to known hosts on the new machine:
   ssh agentydragon@NEW_MACHINE_IP 'ssh-keyscan github.com gitlab.com >> ~/.ssh/known_hosts'

   # From your provisioning machine (with gh installed and authenticated):
   ssh agentydragon@NEW_MACHINE_IP 'cat ~/.ssh/id_ed25519.pub' | \
     gh ssh-key add - --title "HOSTNAME"

   # From your provisioning machine (with glab installed and authenticated):
   ssh agentydragon@NEW_MACHINE_IP 'cat ~/.ssh/id_ed25519.pub' | \
     glab ssh-key add -t "HOSTNAME"

   # Verify both worked from the new machine:
   ssh agentydragon@NEW_MACHINE_IP 'for host in github.com gitlab.com; do echo "Testing $host:"; ssh -T git@$host; done'
   ```

3. Clone ducktape repository and checkout devel branch:

   ```bash
   ssh agentydragon@NEW_MACHINE_IP 'mkdir -p ~/code && git clone git@gitlab.com:agentydragon/ducktape ~/code/ducktape && cd ~/code/ducktape && git checkout devel'
   ```

4. Run the playbook from your provisioning machine:

   ```bash
   cd ansible
   ansible-playbook wyrm.yaml --ask-become-pass
   ```

   When prompted for the BECOME password, enter the sudo password for the agentydragon user on the VM.

5. Run `home-manager switch --flake ~/code/ducktape#<hostname>` on the new machine.

See <TODO.md> for remaining tasks (hostname setup, `gh`/`glab` auth docs).
