# Ansible TODO

## Remote Desktop Infrastructure (Stale)

**Branch**: `authentik-remote-desktop`
**Status**: Stale — Authentik is now deployed in the k8s cluster, user provisioning is
handled via Authentik SSO blueprints. The old VPS is being decommissioned. If remote
desktop is needed, deploy Guacamole or similar in the cluster behind Authentik SSO.

- [ ] Remote desktop server selection and setup (Guacamole in cluster)
- [ ] Integration between Authentik and remote desktop

## Nix/Home-Manager Migration

### Systems Using Home-Manager

- **wyrm** - deployed 2025-08-28
- **atlas** - deployed 2025-08-30
- **agentydragon** - deployed 2025-08-31

### Legacy Systems (without Home-Manager)

- **gpd** - uses `legacy_without_home_manager/*` roles
- **vps** - uses `legacy_without_home_manager/*` roles

### Migration Pattern

Tools migrated to Nix are provided by:

- **Home-manager systems**: Via `nix/home/home.nix`
- **Legacy systems**: Via `roles/legacy_without_home_manager/*` roles

The `legacy_without_home_manager/` roles contain Ansible fallbacks for tools that home-manager provides on migrated systems.

## Other TODOs

- [ ] Update to latest Ansible version
- [ ] Migrate deprecated modules
- [ ] Add molecule tests for roles
- [ ] Document vault variable requirements
