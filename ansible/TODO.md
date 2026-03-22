# Ansible TODO

## Remote Desktop Infrastructure (Stale)

**Branch**: `authentik-remote-desktop`
**Status**: Stale — Authentik is now deployed in the k8s cluster, user provisioning is
handled via Authentik SSO blueprints. The old VPS is being decommissioned. If remote
desktop is needed, deploy Guacamole or similar in the cluster behind Authentik SSO.

- [ ] Remote desktop server selection and setup (Guacamole in cluster)
- [ ] Integration between Authentik and remote desktop

## Nix/Home-Manager Migration

See <../nix/home/migration_plan.md> for full status. GPD is the last legacy holdout.

## VM Provisioning Docs

- [ ] Set hostname on the VM (currently using IP address)
- [ ] Document how to set up `gh` authentication on new machines
- [ ] Document how to set up `glab` authentication on new machines

## Other TODOs

- [ ] Update to latest Ansible version
- [ ] Migrate deprecated modules
- [ ] Add molecule tests for roles
- [ ] Document vault variable requirements
