# Claude Desktop local-proxy rollout (historical)

The local-interception behavior below describes the investigation rollout, not
the current source contract. Hosts now configure only the transport relay;
see [client transport and migration](../../devinfra/github_api_capture/README.md#client-transport).
The recorded runtime bridges still require explicit, ownership-checked retirement.

Investigation: [#5213](https://github.com/agentydragon/ducktape/issues/5213).
Current measurements and causal limits live in
[the September 5 investigation note](attribution_2026_09_05.md).
This file describes the launch and capture contract, not a second incident log.

## One normal launch route

Wyrm2 selects `ducktape.githubApiProxy.desktopPackage` instead of the raw
Claude Desktop package. The normal command, explicit `claude-desktop-proxied`
alias, desktop actions, and `claude://` handler all use the same absolute
wrapper. [PR #5675](https://github.com/agentydragon/ducktape/pull/5675) adds
rugged to this explicit package selection and opts both hosts into the temporary
cloud GitHub mitigation below. Other hosts keep their existing selections.
The regular Claude Code CLI is not made always-proxied by this Desktop-only change.

The wrapper preserves the normal `~/.config/Claude` profile and sign-in state.
It starts the user proxy service and checks its listener via mitmproxy's local
`mitm.it` page before launching. A failed proxy prevents a successful normal
launch; stopping the proxy while Desktop runs interrupts proxied traffic.

The normal desktop identity remains `com.anthropic.Claude.desktop`. A competing
temporary handler did not work reliably because Desktop re-registers that
normal identity at startup. Wrapping all of the normal entry's `Exec` routes
lets its own registration and Electron's single-instance handoff work together.
The same-process URI path and authenticated GitHub traffic were verified in
the live experiment; opening a window alone was not treated as that proof.

## App-private certificate trust

Installed Desktop 1.40609.1 uses Electron `net.fetch` for its native
`GhRestClient`. A Node/Go proxy environment does not configure that Chromium
route. Its startup guard accepts `--proxy-server`, but rejects certificate
verification bypasses and several debugging switches.

The wrapper passes `--proxy-server` and provides the existing proxy CA in a
private NSS database under
`~/.local/state/github-api-proxy/claude-desktop/nssdb`. A Bubblewrap mount makes
that database appear at `~/.pki/nssdb` only inside the app's process tree.
`HOME`, the normal app profile, device access, and system-keyring access remain
unchanged. No global browser trust database is modified and certificate
verification remains enabled.

`--user-data-dir` alone does not isolate Chromium trust. The pinned Chromium
code chooses an existing `~/.pki/nssdb` before its XDG fallback. The ordinary
NSS database's file hashes remained unchanged in the controlled launch tests.

Source references: [Chromium NSS selection](https://chromium.googlesource.com/chromium/src/+/refs/tags/148.0.7778.280/crypto/nss_util.cc),
[Electron initialization](https://github.com/electron/electron/blob/v42.10.0/lib/browser/init.ts),
[Linux protocol registration](https://github.com/electron/electron/blob/v42.10.0/shell/browser/browser_linux.cc).

## Runtime bridge versus host activation

The live rollout deliberately did not activate the dirty primary host
configuration. Instead, the exact generated package was installed in the
previously empty user Nix profile as `claude-desktop-github-proxy`.
`~/.local/share/applications/com.anthropic.Claude.desktop` was absent and now
points to that package's generated entry. This supplies the same normal
routes until the reviewed NixOS configuration is activated.

After NixOS activation installs the wrapped package, remove only the runtime
bridge: `nix profile remove claude-desktop-github-proxy`, then remove the local
desktop-entry symlink **only after checking that it still targets this bridge
package**. Preserve any subsequent operator replacement. Do not remove the
user profile directory, app profile, NSS database, or raw captures.

Removing the bridge before host activation restores the previous launch
routes; an already running proxied instance stays proxied until explicitly
quit and relaunched. This is a package override, not a network firewall that
prevents deliberately launching an old raw Nix-store executable.

## Capture scope and durability

The proxy currently decrypts and records all proxied application traffic,
including unrelated Anthropic traffic. The source flow file is sensitive
even when a derived report is sanitized. Its directory/file are owner-only;
[PR #5670](https://github.com/agentydragon/ducktape/pull/5670) makes those
permissions the service default.

`github-api-proxy-report` produces safe metadata for direct `api.github.com`
requests. It is not a complete inventory of GitHub work: Claude's frontend
also calls cloud-mediated GitHub routes on `claude.ai`. The incident note
records that independently observed coverage gap. Missing `rateLimit.cost`
is unknown; a shared-account header delta is not a per-request charge.

Mitmproxy's bare `-w path` truncates an existing capture at startup. Its
`-w +path` appends; the report still reads the ordinary path without `+`.
[PR #5673](https://github.com/agentydragon/ducktape/pull/5673) corrects the
service's restart behavior. Before any restart, verify the actual unit or
preserve the existing capture in a separately named file. Do not assume an
unmerged configuration fix protects the live file.

Always-on capture currently has no age/size rotation. Bounded retention or
narrower recording is a separate operational decision; no existing evidence
was deleted and the recording scope was not silently narrowed during rollout.

## Temporary cloud GitHub mitigation

PR #5675 adds an independently default-off block for POST requests to host
`claude.ai`, pathname `/v1/code/github/batch-branch-status` before the query
string. Wyrm2 and rugged explicitly opt in; unrelated routes remain allowed.
The operator approved reboot persistence until explicitly disabled. This is a
temporary status-polling degradation while attribution and the multi-day quota
acceptance window continue, not the final upstream polling fix.

On wyrm2 the scoped live bridge is the owned persistent user-service override
`~/.config/systemd/user/github-api-proxy.service.d/90-cloud-github-block.conf`.
Its generated command preserves append mode. Private append-only
`~/.local/state/github-api-proxy/cloud-github-block-events.jsonl` records
startup, thirty-second heartbeat, cumulative blocked count, and shutdown.
It contains no request bodies, session IDs, credentials, or raw query strings.
Separate process lifetimes and known synthetic probes when computing totals.

The earlier runtime append guard also remains at
`/run/user/1001/systemd/user/github-api-proxy.service.d/80-graphql-5213-append.conf`.
After reviewed NixOS activation, verify declarative append mode and the intended
block value, then remove **both owned overrides**, not just `90`: otherwise
`80` overrides the newly declarative service with its older pass-through
command. Preserve any operator replacement and reload the user manager.

Before host activation, removing the verified owned `90` override, reloading,
and restarting intentionally restores the append-safe `80` pass-through
command. That is the live rollback; it does not remove captures or app data.
Source rollback disables the block opt-in but keeps the proxy/capture wrapper.
Rugged has no verified runtime bridge or activation in this investigation.

The generated override and its pinned runtime are protected from garbage
collection by two owned indirect GC roots in the private state directory:
`cloud-github-block-gcroot` and `cloud-github-block-runtime-gcroot`. Remove these
only after normal activation owns the intended service and both overrides have
been retired. Merely placing a Nix-store symlink in a service drop-in does not
make the generated artifact a GC root. Capture/event files are not cleanup targets.

## Central HTTPS proxy compatibility boundary

The operator's preferred central replacement is an HTTPS forward-proxy hostname
with SOPS-managed passwords; Nebula is not required. No central service or
client migration has been deployed. Keep the verified local block until the
central route has authenticated, captured, and blocked real Desktop traffic.

A synthetic loopback probe of installed mitmproxy 12.2.3 established HTTPS-proxy
TLS, authenticated CONNECT, and intercepted HTTPS with both certificate checks
enabled. Correct test credentials returned 200; absent/wrong credentials
returned 407. The fixture answered locally without contacting an external
origin; the disposable listener was stopped afterwards.

Built-in proxy authentication removes `Proxy-Authorization` from forwarded
requests but retains the plaintext password in `flow.metadata["proxyauth"]`.
The probe confirmed both behaviors. A central raw-capture writer must scrub
that metadata before persistence, retaining only an approved client identifier.

Installed Desktop 1.40609.1 does not register proxy-login handlers. Electron
42.10.0's `net.fetch` takes a per-request authentication path: adding only an
`app.on("login")` handler does not cover its GitHub client. Chromium also ignores
credentials embedded in manual proxy URLs. A transport-only local relay or
tested Desktop packaging authentication wiring is still required; neither is
implemented. Existing packaging does not modify `app.asar`.

Source: [Electron fetch implementation](https://github.com/electron/electron/blob/v42.10.0/lib/browser/api/net-fetch.ts),
[request authentication callback](https://github.com/electron/electron/blob/v42.10.0/lib/common/api/net-client-request.ts#L469),
and [Chromium proxy credentials](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md#proxy-credentials-in-manual-proxy-settings).
