# Authentik Remote Desktop Infrastructure (WIP)

This branch contains work-in-progress components for setting up a VPS-based remote desktop environment gated by Authentik SSO.

## Vision

Create a secure remote desktop environment on the VPS that:
- Provides full desktop environment accessible via browser
- Is protected behind Authentik OAuth/SSO authentication
- Only accessible through WireGuard VPN for additional security
- Allows centralized user management and 2FA

## Current Status

**🚧 UNFINISHED - DO NOT DEPLOY TO PRODUCTION 🚧**

### What's Implemented
- Basic Authentik role structure
- Docker Compose deployment configuration
- Nginx reverse proxy setup
- WireGuard-only access configuration

### What's Missing
- Remote desktop server configuration (likely using Guacamole or similar)
- Integration between Authentik and remote desktop solution
- User provisioning automation
- Desktop environment setup (XFCE/MATE/etc)
- Performance tuning for remote desktop over WireGuard
- Backup and disaster recovery procedures

## Architecture Overview

```
Internet -> WireGuard VPN -> Nginx -> Authentik SSO
                                    -> Remote Desktop Server
```

1. Users connect via WireGuard VPN
2. Access `auth.agentydragon.com` for authentication
3. After successful auth, redirected to remote desktop
4. Desktop sessions isolated per user

## Files in This Branch

- `ansible/roles/authentik/` - Authentik deployment role
- `ansible/BT-HA-Reporter.xml` - Bluetooth HA reporter config (unrelated)
- `ansible/tasker/` - Tasker automation files (unrelated)
- `ansible/vps.yaml.orig` - Original VPS playbook with authentik commented out

## Next Steps

1. Choose and implement remote desktop solution
2. Configure Authentik applications and flows
3. Set up desktop environment
4. Implement session management
5. Add monitoring and logging
6. Security hardening
7. Performance optimization

## Security Considerations

- Currently requires manual vault secrets setup
- Ensure strong passwords for PostgreSQL and secret keys
- Regular security updates needed
- Consider implementing rate limiting
- Add fail2ban rules for auth attempts

## DO NOT MERGE

This branch is intentionally kept separate as it represents incomplete infrastructure that could cause issues if deployed prematurely.