## To install roles and collections

```bash
cd ansible
ansible-galaxy role install -r requirements.yaml
ansible-galaxy collection install -r requirements.yaml
```

Roles and collections install into the default `~/.ansible/roles` and
`~/.ansible/collections` paths so third-party content never lands in
`ansible/`.

## To deploy Atlas

```bash
cd ansible
ansible-playbook atlas.yaml
```

`--skip-tags` is unreliable: tasks were not tagged with it in mind.

## To deploy GPD

```bash
cd ansible
ansible-playbook gpd.yaml --ask-become-pass
```

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

   # Verify GitHub worked from the new machine.
   ```

3. Clone ducktape repository and checkout devel branch:

   ```bash
   ssh agentydragon@NEW_MACHINE_IP 'mkdir -p ~/code && git clone git@github.com:agentydragon/ducktape ~/code/ducktape && cd ~/code/ducktape && git checkout devel'
   ```

4. Run the playbook from your provisioning machine:

   ```bash
   cd ansible
   ansible-playbook <hostname>.yaml --ask-become-pass
   ```

   When prompted for the BECOME password, enter the sudo password for the agentydragon user on the VM.

5. Run `home-manager switch --flake ~/code/ducktape#<hostname>` on the new machine.

See <TODO.md> for remaining tasks (hostname setup, `gh`/`glab` auth docs).
