# Ansible TODO

## Nix/Home-Manager Migration

GPD is the last host on the `legacy_gui` role, and the flake has no `gpd` output yet. Once GPD runs
`home-manager switch`, remove the role from `gpd.yaml` and delete `ansible/roles/legacy_gui`. Its GNOME extensions
(`meta/main.yml`) are only partly in Nix (`programs.gnome-shell.extensions` under `nix/home/`): port or drop the rest first.

## VM Provisioning Docs

- [ ] Set hostname on the VM (currently using IP address)
- [ ] Document how to set up `gh` authentication on new machines
- [ ] Document how to set up `glab` authentication on new machines

## Other TODOs

- [ ] Update to latest Ansible version
- [ ] Migrate deprecated modules

- [ ] Remove stale `ansible-vault` libsecret entries from hosts (`secret-tool lookup service ansible-vault account ducktape`)
