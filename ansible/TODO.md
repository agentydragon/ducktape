# Ansible TODO

## Remote Desktop Infrastructure

**Branch**: `authentik-remote-desktop`
**Status**: Work in Progress - DO NOT MERGE

There is ongoing work to implement a VPS-based remote desktop environment protected by Authentik SSO.

See the `authentik-remote-desktop` branch for:
- Authentik identity provider role
- Plans for browser-based remote desktop
- WireGuard-only access configuration

The implementation is incomplete and needs:
- [ ] Remote desktop server selection and setup (Guacamole/Apache Guacamole/etc)
- [ ] Integration between Authentik and remote desktop
- [ ] Desktop environment configuration
- [ ] User provisioning automation
- [ ] Performance optimization over WireGuard
- [ ] Security hardening

**DO NOT** uncomment the authentik role in `vps.yaml` until this work is complete.

## Other TODOs

- [ ] Update to latest Ansible version
- [ ] Migrate deprecated modules
- [ ] Add molecule tests for roles
- [ ] Document vault variable requirements
