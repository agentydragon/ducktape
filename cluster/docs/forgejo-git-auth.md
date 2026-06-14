# Forgejo Git Authentication

Forgejo serves HTTPS at `https://git.allegedly.works` through the cluster HTTP
Gateway. Git-over-SSH is a raw TCP protocol, so it cannot be routed by HTTP
hostname on the existing HTTPS listener. The Forgejo Helm values therefore
advertise SSH on port 2222, and host Home Manager config sets that port in
`~/.ssh/config` for `git.allegedly.works`.

## Current Choice: User SSH Keys

The `agentydragon` Forgejo account uses one dedicated Ed25519 keypair per host:

- `ssh_keys/wyrm2-forgejo.{pub,sops.key}`
- `ssh_keys/rugged-forgejo.{pub,sops.key}`
- `ssh_keys/atlas-forgejo.{pub,sops.key}`
- `ssh_keys/iguana-forgejo.{pub,sops.key}`

The private keys are SOPS binary files decryptable by the admin age key and by
the owning host's user age key. Home Manager installs the decrypted key at
`~/.ssh/agentydragon_forgejo_id_ed25519` and configures:

```sshconfig
Host git.allegedly.works
  User git
  Port 2222
  IdentityFile ~/.ssh/agentydragon_forgejo_id_ed25519
  IdentitiesOnly yes
```

`tf/gitops/forgejo-agentydragon` attaches the public keys to the existing
OIDC-created `agentydragon` user with `forgejo_ssh_key`. The human account
lifecycle stays outside Terraform; only the SSH keys are declarative.

## Alternatives Considered

### Personal Access Tokens

Forgejo can create scoped user access tokens through the API, including
repository-limited tokens. Tokens would work over the existing HTTPS route and
avoid a dedicated SSH TCP path.

The pinned `svalabs/forgejo` provider can authenticate with an API token but
does not provide an access-token resource. Managing PATs would therefore be
manual SOPS state, an ad hoc API/provisioner flow, or a provider extension. PATs
are also bearer secrets that tend to leak through credential helpers, remotes,
environment variables, and logs more easily than SSH private keys.

### Service User Passwords

Several repo modules already create Forgejo service users and store generated
passwords in Kubernetes Secrets for HTTPS Basic auth. This is good for workloads,
but it is not user-level access for the `agentydragon` account.

### Deploy Keys

A deploy key is an SSH public key attached to one repository instead of to a
user account. The private key can clone that repository, and can push only if
the deploy key is created with write access.

Deploy keys are useful for narrow automation such as "this one job can read this
one repo" or "this one deployer can push this one mirror." They are not a good
fit for a workstation identity because each repository needs its own grant and
the key does not inherit normal user permissions across the Forgejo account.

### Dedicated SSH Exposure

A cleaner UX than `ssh://git@git.allegedly.works:2222/...` would be a dedicated
port-22 TCP route or dedicated IP for Forgejo SSH. That is orthogonal to account
credential type: user SSH keys, deploy keys, and SSH-backed service users all
need the same TCP exposure.
