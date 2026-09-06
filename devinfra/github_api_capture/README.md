# GitHub request metadata

`github-api-proxy-report` reads saved mitmproxy flows without replaying requests.
The addon can also be loaded into a live mitmproxy process. It emits JSONL only
for `api.github.com`, retaining timestamps, method, path without query parameters,
user agent, exact query SHA-256, status, GitHub request ID, GraphQL error type/code,
explicit nominal GraphQL cost, and account rate-limit headers. Request duration
can be derived from `started_at` and `completed_at` when completion is available.

It omits auth headers, variables, query text, response payloads, error messages,
and unrelated destinations. User agent is a client hint, not process identity.
Paths can still identify private repositories; review metadata before publication.
Transport failures remain visible without their potentially sensitive error text.

`nominal_graphql_cost: null` means unknown, including responses that do not select
`rateLimit.cost`. It is never zero-filled or inferred from `x-ratelimit-used`.
That header describes the shared account bucket: concurrent callers, reset
boundaries, and [timeout penalties](https://github.blog/changelog/2025-07-21-including-timeouts-in-primary-rate-limits/)
prevent interpreting its difference as this request's cost. An accounting residual
alone cannot prove bypass traffic. Error metadata is retained even on HTTP 200.

The source `github.flows` remains a raw capture containing sensitive unrelated
application traffic. This metadata report does not make the source safe to publish.

## Cloud-mediated GitHub calls

The report also includes three exact `claude.ai` routes: GitHub batch branch status,
compare refs, and installation status. These records include the endpoint, bounded
`caller` tag, exact request-body fingerprint, batch cardinalities, completion/status,
and transport-failure indicator. They exclude session/repository/branch values,
auth headers, other query parameters, and response payloads. Missing cardinalities
remain unknown; an explicitly empty list has cardinality zero.

These are calls to Claude's backend, not observations of its upstream GitHub
requests. The reporter cannot assign GitHub GraphQL cost or query fingerprints
to them. HTTP 200 likewise does not prove every upstream GitHub operation succeeded.

## Optional incremental session WebSocket metadata

`session_ws_metadata.py` is a **default-off, unwired addon**. It is not enabled by
the capture service or report command. Explicit startup opt-in requires both
`record_cloud_session_ws=true` and `cloud_session_ws_events=<private JSONL path>`
when loading this script with mitmproxy. Changing these options requires restart;
this is not an instruction to restart an existing capture.

It observes only `claude.ai` GET `/v1/sessions/ws/:session/subscribe`, discarding the
session path segment and query. It appends start/message/end records immediately,
plus a 30-second heartbeat with active-flow and cumulative message/parse/write-failure
counts. Each process lifetime has its own start timestamp; each observed connection
gets a locally generated ID. Existing JSONL is appended, not truncated; files are
restricted to 0600 and newly created immediate parent directories to 0700.

Records contain direction, message timestamp, byte length, text/binary kind, and
fixed structural counters. They never contain payloads, commands, tool outputs,
headers, original identifiers, arbitrary keys, or unknown type/tool names. Binary,
over-64-KiB text, invalid JSON, analysis-limit, and unknown-schema messages have an
explicit status and unknown (`null`) structural counts. Recognized envelopes with
unrecognized content blocks count those blocks separately. Write failures leave
traffic untouched, emit only a generic errno log, and appear in cumulative counts
if a later append succeeds. Persistent output failure requires checking the proxy
log and missing heartbeats; there is no independent delivery channel.

These are observed message shapes, not unique or executed tool calls. Streamed
tool starts and full assistant messages can describe the same call; the addon does
not retain IDs to deduplicate, reassemble input deltas, or inspect commands. It
cannot identify which tools issued GitHub requests or assign GraphQL cost. Mitmproxy
reassembles fragmented WebSocket frames before this hook; control frames are not
message observations. Connections already open before addon startup are not covered.

The loopback integration test holds one real WebSocket open across multiple text
and binary exchanges and reads its metadata before close. It also flushes mitmproxy's
Save stream and verifies the open flow is absent, then verifies the completed raw
flow after close/shutdown. Appending closes the metadata file each time for immediate
reader visibility; this is not an fsync/power-loss durability guarantee. Neither
this addon nor its tests activate live capture, modify messages, or contact an account.

## Client transport

<../../nix/home/modules/github-api-proxy.nix> owns the loopback proxy and the
normal Claude Desktop executable, desktop actions, and OAuth URI launch routes.
The application profile and sign-in state stay in their normal locations.

### Remote mode

`ducktape.githubApiProxy.remote.enable` defaults to `false`. When enabled, the
same `github-api-proxy.service` runs Squid as a transport-only relay, replacing
local interception. It authenticates to one HTTPS parent, verifies the parent's
hostname against the standard CA bundle, and never falls back to an origin.
Only the central service intercepts or captures. The relay has no disk/content
cache, access/flow logs, or signing key. Local capture reports are not installed.

`remote.credentialsFile` must name an owner-only runtime file, not a Nix-store
path. Its JSON object contains exactly one client identifier matching
`[A-Za-z0-9._-]{1,64}` and a 32-byte lowercase hexadecimal password:

```json
{
  "example-desktop": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

For the per-host SOPS Kubernetes Secret, Home Manager can select
`key = "stringData/credentials.json"` and pass `config.sops.secrets.<name>.path`.
Use the host's own file and recipient; do not install another host's credentials.
The renderer writes Squid's secret-bearing configuration into the service's
owner-only runtime directory. Credentials never enter the Nix store, arguments,
or logs. Squid diagnostics can echo configuration, so its output is discarded;
unit status and the bounded authenticated launcher probe expose failure.

`remote.caCertificate` is a verified, pinned **public** central interception CA
PEM. It is distinct from the public roots used for the outer HTTPS proxy hop.
Desktop trusts it only through the existing app-private NSS mount; proxied CLI
processes receive a private combined bundle. Do not fetch-and-trust a certificate
at startup or modify global browser trust. The private signing key stays central.

The launcher probes `http://mitm.it/` through the relay. The central endpoint must
answer this request only after outer TLS and Basic authentication, with a bounded
constant `200` response and no origin request. A listening local socket alone is
not readiness. Missing credentials, rejected authentication, unavailable parents,
and untrusted or mismatched certificates fail closed.

The relay is not a host firewall: applications that do not select this proxy and
Chromium's normal loopback bypass remain outside its coverage.
The authenticated identifier names the configured credential, not a process.
Desktop and opt-in `claude-proxied` sessions share this relay and credential.

### Workstation ownership

The host opt-ins are <../../nix/home/hosts/wyrm2.nix> and
<../../nix/home/hosts/rugged.nix>: `github-proxy.allegedly.works:8443` is their
single parent. Each selects its own
`cluster/k8s/github-api-proxy/secrets/<host>-credentials.sops.yaml`, key
`stringData/credentials.json`. Home Manager's existing `sops-nix.service` decrypts
it as the configured user into `%r/secrets.d`, mode `0600`, with a stable
`~/.config/sops-nix/secrets/github_api_proxy_credentials` symlink. The relay is
ordered after and requires that service. On credential rotation, reconcile the
central Secret, activate the corresponding Home Manager configuration, then
restart the relay so its private runtime configuration receives the new value.

<../../nix/home/modules/github-api-proxy-ca.pem> pins only the public
`tls.crt` from Secret `github-api-proxy/github-api-proxy-interception-ca`.
Its SHA-256 fingerprint is
`6F:51:AD:39:F5:B9:7D:B7:E2:FB:6D:96:50:91:70:7F:74:CC:05:CB:1F:DB:90:6E:36:D2:A1:8E:3B:54:0C:55`.
Certificate renewal requires explicit pin verification and app trust migration;
the launcher never downloads a replacement. The cluster Certificate owner is
<../../cluster/k8s/github-api-proxy/identity/certificates.yaml>.

### Migration and retirement

Do not activate remote mode until the central authenticated readiness and real
application route are verified. Set `blockCloudGithubBatch = false` locally;
central interception owns any remaining mitigation policy.

Both hosts use NixOS-inline Home Manager: the normal deployment owner is
`nixosConfigurations.<host>`, not a standalone `home-manager switch`. Build and
inspect its generated `home-manager.users.agentydragon` proxy unit and Desktop
package before a scoped activation. A full `nixos-rebuild switch` also activates
unrelated configuration changes; review that generation separately.

After central end-to-end verification, inventory and remove only the owned local
service overrides (including the temporary `80`/`90` overrides), profile bridge,
unused local CA signing keys, and diagnostic Nix GC roots. Verify exact targets
and get approval before deletion. Preserve raw investigation captures and the
normal Desktop profile/sign-in. Verify that the single service now runs only the
transport relay and no old local MITM process remains. Once all actual consumers
have migrated, remove the local-interception mode and its unused dependencies.

The known wyrm2 override targets are
`~/.config/systemd/user/github-api-proxy.service.d/90-cloud-github-block.conf`
and `/run/user/1001/systemd/user/github-api-proxy.service.d/80-graphql-5213-append.conf`.
Either can replace the declarative `ExecStart`; inspect both before the switchover
and do not restart a bare `-w` capture command. The owned
`~/.local/state/github-api-proxy/cloud-github-block{,-runtime}-gcroot` links are
retired only after these overrides no longer need them. Inspect the actual
profile bridge and local CA files on each host before deciding exact cleanup
targets; neither source activation nor a successful build removes these.

### Validation

`bbr test //devinfra/github_api_capture:test_relay_config` runs real Squid on RBE
against a synthetic TLS-authenticated parent and independent HTTPS origin.
It checks successful nested TLS and authenticated readiness, no leaked parent
authorization at the origin, rejection of wrong credentials or bad parent TLS,
and absence of direct origin fallback when the parent is unavailable.

The test image uses Debian `squid-openssl` `7.6-2`, pinned by the generated
`squid.lock.json`; the host service uses the repository-pinned Nix Squid `7.6`.
The test also executes the actual CA preparation script against an old bundle
and NSS nickname before switching to a new certificate with an old timestamp.
Validate the rendered Home Manager unit and the pinned host binary before rollout
as well.
