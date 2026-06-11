# Remote Desktop SSO Requirements

## Objectives

- Single browser login (via Autentik) should give me access to a remote Linux desktop with no additional credential prompts.
- Identity attributes from Autentik (username, groups) must propagate all the way to the desktop session for authorization/auditing.
- Avoid per-host password storage in Guacamole/Teleport; instead rely on short-lived credentials (Kerberos tickets, mTLS certs, etc.).

- ## Constraints & Preferences

- Prefer open-source / self-hosted components; avoid solutions that require paid enterprise licenses.
- Authentik is the IdP of record; new tooling must integrate via OAuth/OIDC or LDAP/RADIUS.
- Existing stack includes Guacamole + Autentik + Kubernetes; prototypes should run inside the cluster without touching production hosts.
- Will tolerate per-host agents / controllers if they enable true SSO, but simpler infrastructure is preferred when possible.

- ## Desired Features

- Browser-based portal listing available Linux desktops.
- MFA via Autentik nice-to-have, but not a strict requirement.
- Session reuse: once authenticated, Guacamole/Teleport should reuse the same identity to prove itself to the desktop (Kerberos delegation or short-lived host certs).
- Minimal manual user sync—ideally pull users/groups from Autentik automatically.
- Provide a fallback path (local password prompt) if the SSO layer or ticket issuance fails, so access is still possible during outages.

## Open Questions

- Kerberos vs mTLS certificates: which path gives the smoother UX with our current tools?
- Can we tolerate an additional controller (e.g., Teleport OSS + manual user sync) or do we need a pure Guacamole solution?
- How do we handle credential fallback if the SSO layer is unavailable (Kerberos down, Teleport offline)?
