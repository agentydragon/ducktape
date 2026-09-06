# Central GitHub API observation proxy

This is the central replacement for workstation interception proxies. It also
observes cloud-mediated GitHub work on Claude endpoints: a direct GitHub-only
filter would not cover that traffic. Do not reuse the agent-sandbox proxy's CA,
credentials, or network authorization boundary for workstation traffic.

## Transport and identity

`github-proxy.allegedly.works:443` is an authenticated HTTPS forward proxy.
The shared Gateway passes the TLS stream through a hostname-specific TLSRoute;
an ordinary HTTPRoute is not assumed to forward CONNECT requests.

There are two distinct certificate roles:

- The public proxy endpoint uses a normal publicly trusted server certificate.
  Clients verify that certificate and hostname before sending proxy credentials.
- A dedicated interception CA signs inner-origin certificates. Its private key
  stays in the cluster. Only its verified public certificate is installed in the
  selected application's private trust store; no global browser trust change.

Each host has its own SOPS-backed Basic credential. This is an explicit
noninteractive proxy-authentication surface, not an Authentik browser UI.
The server authenticates before forwarding, accepts no plaintext credential
transport, and removes proxy credentials from both headers and flow metadata
before saving raw captures. Metrics use configured client IDs, not user-supplied
identity labels or arbitrary paths.

For clients without usable proxy authentication, a loopback-only transport
trampoline adds the credential and connects over verified TLS. It does not
intercept, capture, cache response content, hold an interception signing key, or
fall back to a direct origin connection. The normal Desktop profile and OAuth
callback identity must remain unchanged.

## Credential ownership

The planned SOPS sources are `secrets/wyrm2-credentials.sops.yaml` and
`secrets/rugged-credentials.sops.yaml` beneath this directory. Each is one Secret
whose `stringData.credentials.json` contains a single fixed-client-ID/password
mapping. Flux and the corresponding host's user identity consume that same file;
one host is not granted decryption of the other's credential. The central runtime
reads both mounted JSON files and rejects duplicate IDs. Secrets stay out of the
Nix store, command arguments, metrics and ordinary logs.

## Migration gate and cleanup

The central runtime and host trampoline are being implemented independently.
This directory is not evidence of deployment. Preserve the working local
mitigation until all of these are verified on the actual central route:

1. Correct credentials and both TLS trust chains work; wrong or missing
   credentials, untrusted proxy TLS and unreachable upstream fail closed.
2. Real Desktop traffic is attributed to the selected host, the existing exact
   cloud batch-status block still works, and unrelated application traffic and
   OAuth callbacks work through the normal launch path.
3. Request metrics and incremental stream metadata arrive centrally; raw capture
   appends safely, remains private, and contains no proxy password metadata.

Then retire the previous local interception service/configuration, owned runtime
overrides, temporary package/profile bridges, unused local interception private
keys and owned Nix GC roots. Inspect each exact target before cleanup and retain
operator replacements. Preserve raw investigation captures and the Desktop
profile/sign-in state. Do this per host: a source opt-in alone does not prove
rugged or wyrm2 migrated successfully.

Successful migration is not resolution of quota exhaustion. Account-wide quota
and observation coverage still require the agreed multi-day acceptance window.
