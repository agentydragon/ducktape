# Tana MCP: getting rid of the noVNC sign-in step

## Problem

`k8s/agents/tana-mcp/` runs Tana Desktop in a container under Xvfb so we can host
its local MCP server cluster-internally. Whenever the in-pod Firebase session
goes away (Tana revokes it, the PVC is wiped, the user signs out elsewhere, etc.)
the only documented recovery is:

1. `kubectl port-forward` the noVNC port,
2. open `vnc.html` in a host browser,
3. inside that virtual desktop, drive an actual Chromium-class browser through
   a Google OAuth flow against `app.tana.inc`,
4. let Tana relaunch and turn the Local API back on.

This is the single most annoying maintenance touch on the cluster. The cause is
that Tana Desktop will only accept a sign-in by way of the `tana://auth?...`
deep-link that the official web client builds at the end of its Firebase login
flow, and the official web client wants to run inside a real GUI browser.

This note collects options for replacing that with something we can drive from
a normal host shell (or fully automate).

## Background — how Tana Desktop actually authenticates

From the reverse-engineered desktop and web bundles in
`../../gaffer-private/tana/re/`:

1. The desktop main process registers `tana://` as a custom protocol and handles
   it in both `open-url` and `second-instance` hooks. `kDe` parses
   `tana://auth?token=<X>&providerId=<Y>` and emits an `("auth", { token, providerId })`
   IPC into the renderer
   (`tana/upstream/desktop/snapshots/v1.515.0/build/main.js:87706`).
2. The renderer reacts to that IPC in `desktopEventBridge.setupElectronHostBridge`.
   If `providerId === "tanaFirebaseToken"`, it calls
   `signInWithCustomToken(getAuth(), token)` directly (`v1(Mi(), token)` in
   `tana/re/web/.../app/desktop/desktopEventBridge.js:158`).
3. The "click Sign in with Google" button in the in-app login screen just sends
   an `auth_request` IPC, which is implemented in main as
   `vt.shell.openExternal("${appUrl}/electron?useFirebaseAuthToken=true")`. So
   the desktop is not doing Google OAuth itself — it's just delegating to a
   web browser, then waiting for Tana's web client to round-trip a custom token
   back via the `tana://auth?...` deep link.
4. The web client's `signInSuccessWithAuthResult` branch with
   `useFirebaseAuthToken=true` calls Tana's own `fetchCustomToken` callable
   (`integrations/firebase/cloud_functions/client.js`), gets back
   `{ data: { customToken } }`, and navigates to
   `tana://auth?token=<customToken>&providerId=tanaFirebaseToken`
   (`app/auth/electronAuth.js`).
5. Firebase JS SDK, on the renderer side, persists the resulting refresh
   token into IndexedDB at `~/.config/Tana/IndexedDB/<origin>/firebaseLocalStorageDb`.
   That refresh token is what survives pod restarts via the PVC; it is also
   what gets invalidated when sign-in eventually breaks.

So the operational chokepoint is just the `tana://auth?token=...&providerId=tanaFirebaseToken`
URL. Anything that can produce that URL and hand it to the in-cluster Tana
binary completes sign-in without noVNC. The Firebase custom token is short-lived
(~1h), but the refresh token Firebase derives from it after `signInWithCustomToken`
is long-lived, so this only has to be done occasionally.

## Option A — laptop CLI that captures `tana://auth?...` and pipes it into the pod

Run a small CLI on your normal workstation that does, in one shot:

1. Open `https://app.tana.inc/electron?useFirebaseAuthToken=true` in your real
   default browser. You're already signed into Google there, so the Google
   OAuth step is one click ("Continue as agentydragon"). No headless browser
   automation, no captcha exposure.
2. Intercept the `tana://auth?...` redirect at the end of the flow.
3. Run `kubectl exec -n tana-mcp deploy/tana-mcp -c tana-desktop -- /usr/bin/tana 'tana://auth?token=...&providerId=tanaFirebaseToken'`.
   Inside Electron, that invocation triggers the `second-instance` handler,
   `kDe` parses the URL, and the renderer signs in with the custom token. Done.

Two ways to do step 2:

- **xdg-mime handler.** Register a desktop entry (e.g. `tana-cluster-capture.desktop`)
  as the handler for `x-scheme-handler/tana` while the CLI is running, then
  restore the previous handler. Pro: works with any real browser; user only
  signs in once. Con: temporarily steals the `tana://` handler on the host,
  which interferes with a host-installed Tana Desktop if you ever run one.
- **Browser-driven capture.** Spawn a Chromium/Playwright window with
  `--user-data-dir` pointing at a throwaway profile, but pre-seed it with your
  Google account via the headed `chrome --auth-server-whitelist`-style flow.
  Listen for the navigation attempt to `tana://...` (Playwright surfaces it as
  a navigation error or `request` event) and grab the URL. Pro: nothing
  registered on the host. Con: you have to either type your Google password
  in a fresh profile or share `~/.config/google-chrome` with the script.

Either way, this is the lowest-architecture-change option: zero new pods, zero
new secrets. It's just a one-button-per-incident replacement for noVNC.

### Operational shape

- Ship the CLI as a `bb run //tana/mcp_server:relogin` or a tiny `tools/`
  binary. Auth happens against the user's local kubeconfig (not the in-cluster
  agent kubeconfig).
- Document it in `k8s/agents/tana-mcp/README.md` § "Initial Setup" as the
  primary recovery path; keep noVNC as the fallback for cases where the URL
  capture fails.

## Option B — store the Firebase refresh token in SOPS and re-seed the pod

Skip the deep-link dance entirely. Once you have a Firebase refresh token,
you can re-mint Tana's session indefinitely from a server with no browser
involved.

### Why the refresh token alone is enough

Firebase Auth treats custom-token sign-in identically to any other sign-in
path. The chain that matters:

```text
project service account private key       ← only Tana's backend has this
  └─ signs custom token (1h JWT, any uid)
       └─ POST identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken
            └─ {idToken (~1h), refreshToken (long-lived)}
                 └─ POST securetoken.googleapis.com/v1/token (refresh grant)
                      └─ {new idToken, occasionally-rotated refreshToken}
```

So with one refresh token we can always produce a fresh ID token via
`securetoken.googleapis.com`, then call Tana's `fetchCustomToken` Cloud
Function (which only requires a valid Firebase ID token) to mint a brand-new
custom token, then hand that custom token to the in-pod renderer through
`signInWithCustomToken(...)`. Tana's renderer treats the resulting session
as identical to a "real" Google sign-in.

Crucially, **`signInWithCustomToken` issues its own fresh refresh token
server-side**, which Firebase JS SDK persists in IndexedDB. From then on the
in-pod Tana manages its own refresh token; our SOPS-stored one is only
needed again when the in-pod session is broken.

### Bootstrap (no browser required)

The one-time bootstrap that produces the SOPS-encrypted refresh token does
not need Chrome, Playwright, or any browser automation. If you have Tana
Desktop installed locally (signed into the right account), its IndexedDB
already contains a refresh token.

Run:

```bash
bb run //tana/firebase_session_extractor -- \
    ~/.config/tana-outliner/IndexedDB/https_app.tana.inc_0.indexeddb.leveldb \
  | sops -e --input-type binary --output-type yaml \
      /dev/stdin > k8s/agents/tana-mcp/tana-firebase-refresh-token.sops.yaml
```

`tana/firebase_session_extractor/` is a small Rust binary that:

1. Opens the leveldb dir under the user-data path of a local Tana Desktop
   install, using `rusty-leveldb` with the `idb_cmp1` comparator that
   Chromium IndexedDB uses.
2. Finds the `firebase:authUser:<API_KEY>:[DEFAULT]` record by scanning
   values for the marker string (Firebase JS SDK writes the key as a literal
   ASCII string inside the V8 structured-clone payload).
3. Decodes the V8 structured-clone blob with `v8_valueserializer` (Deno's
   pure-Rust port of V8's `ValueDeserializer`).
4. Walks `value.stsTokenManager.refreshToken` and prints it to stdout.

For setups that don't run Tana Desktop locally, fall back to:

- **Devtools paste.** In any signed-in Tana web tab, run
  `JSON.stringify(window.fbauth().getAuth().currentUser.stsTokenManager.refreshToken)`
  and pipe it into `sops -e`. The web app exposes `window.fbauth()` as a
  debug helper that wraps `getAuth()`.
- **Direct Firebase REST sign-in** (if you set a password on the Firebase
  account): `POST identitytoolkit.googleapis.com/v1/accounts:signInWithPassword`
  with `{email, password, returnSecureToken: true}` returns `{refreshToken}`
  directly.

The Firebase web API key for project `tagr-prod` is
`AIzaSyA9LtJM6Ga9VAwCfj9w_mNORdOaq2yLshQ` — Firebase web keys are not
secrets (Google explicitly treats them as public identifiers), so it can
live in the sidecar's config and tracked in git.

### Steady state

A small in-pod sidecar (`tana/firebase_resigner/`) does the loop. Most of
the time it does nothing.

1. Watches some "is the in-pod Tana signed in" health signal (e.g. a
   tightened readiness probe on the proxy, or a periodic call to the local
   MCP server's `tools/list` to confirm the renderer has a workspace).
2. When the signal flips negative:
   - `POST securetoken.googleapis.com/v1/token?key=<API_KEY>` with the SOPS
     refresh token. If the response includes a rotated refresh token, write
     it back to the K8s Secret so the SOPS material stays current.
   - Use the resulting ID token (Bearer auth) to call the Cloud Function
     URL for `fetchCustomToken` (the web bundle uses `europe-west1`).
   - Deliver the resulting Firebase custom token to the desktop container
     via either:
     - **The `tana://auth` deep-link.** A few lines in
       `tana/mcp_server/entrypoint.sh` add a localhost-only HTTP receiver
       (e.g. a 10-line `http.server` subclass) that accepts
       `POST /reseed { "url": "tana://auth?token=...&providerId=tanaFirebaseToken" }`
       and `exec`s `/usr/bin/tana <url>`. The sidecar talks to it over pod
       loopback. Electron's `second-instance` handler delivers the URL
       into the running renderer, which calls `signInWithCustomToken(...)`.
       This is the lowest-coupling path because it goes through Tana's
       officially-supported sign-in API.
     - **Direct IndexedDB write.** The init container materializes the
       `{ fbase_key, value: <User> }` record using the same structured-
       clone format we just decoded. Cleaner because it boots straight
       into a signed-in state with no IPC dance, but requires re-serializing
       the V8 envelope, which is brittle to Firebase JS SDK version bumps.
       Start with the deep-link path and only move to direct injection if
       it turns out the entrypoint receiver is the wrong place to put
       this glue.

### Pros

- Fully chrome-less bootstrap. The Rust extractor reads from a local Tana
  Desktop install with no browser, no Selenium, no captchas.
- Zero-touch in steady state: sidecar acts only when needed.
- Refresh-token rotation is handled by the sidecar, so we don't carry a
  stale token forever.

### Cons

- New moving part (a sidecar + a small entrypoint receiver) on top of the
  current deployment.
- Bootstrap assumes you have Tana Desktop installed locally. Falls back to
  the devtools paste, but that's still a manual step.
- If Tana ever moves Firebase Auth persistence off the standard JS SDK
  storage path (unlikely but possible), the extractor breaks. We'd be back
  to the devtools-paste path until the extractor is updated.

## Option C — keep the in-pod broker, but consume `tana://auth?...` from outside

A hybrid of option A's "deliver `tana://auth?...` from outside" with a
cluster-internal receiver instead of `kubectl exec`. Run a small HTTP
receiver sidecar in `tana-mcp` (cluster-internal only) that accepts
`POST /deeplink { url: "tana://auth?..." }`. On receipt, it
`exec`s `tana <url>` against the running Tana process via the desktop
container's PID 1 (or just spawns a new instance — Electron's
`second-instance` handler is the supported delivery path).

This is just option A with the kubectl-exec step replaced by a NetworkPolicy-
limited HTTP call from a workload that already has cluster access (e.g. from
`agent-cli` or from the existing nginx-auth-proxy fronting `/mcp`). The win
over A is that you don't need kubectl on the laptop that's doing the capture
— anything that can reach the in-cluster HTTP receiver is enough.

It's strictly more moving parts than A, so only worth doing if we eventually
want the capture-and-relogin step to happen from somewhere other than the
operator's laptop (e.g. from a scheduled job on `wyrm2` that re-asserts the
session weekly).

## Option D — move Tana Desktop off the cluster

If we accept that "Tana Desktop in a container" is forever fighting Tana's
desktop-Google-OAuth flow, an alternative is to stop running it in the
cluster at all:

- Run Tana Desktop in a long-lived user session on `wyrm2` (or any always-on
  host). Sign-in happens through the normal host login flow; the session
  lives in the user profile, not a PVC.
- Expose its `localhost:8262` MCP server via the same Host/Origin-rewriting
  nginx config we already have in `k8s/agents/tana-mcp/`, but running as a
  systemd-managed nginx on the host (or a tiny in-cluster nginx that proxies
  via Nebula back to the host).

Pros:

- Eliminates the entire reason the noVNC step exists. Native desktop sign-in
  is, by construction, the one Tana actually tests.
- No PVC, no Xvfb, no startupProbe gymnastics.

Cons:

- Loses the "everything is k8s" property and makes the MCP endpoint depend on
  a single non-cluster host being up.
- Doubles operational surface (a systemd unit on `wyrm2` + the in-cluster
  facade) for a service whose whole appeal was that the cluster handles
  uptime.

This option is reasonable if option A turns out to also keep breaking — i.e.
if Tana keeps changing its OAuth handoff in ways that defeat URL capture.

## Recommendation

Option B is the target — a chrome-less bootstrap plus a server-side re-seed
loop gives us zero-touch recovery, and the bootstrap-side complexity is
already paid (the Rust extractor under `tana/firebase_session_extractor/`
works end-to-end).

Sequence:

1. **Land the extractor** (done). One-time bootstrap into a SOPS-encrypted
   K8s Secret.
2. **Add the entrypoint receiver** to `tana/mcp_server/entrypoint.sh` so the
   desktop container exposes a localhost-only `POST /reseed` endpoint that
   delivers a `tana://auth?...` URL into the running Electron.
3. **Write the sidecar** (`tana/firebase_resigner/`). Tightened readiness
   probe + the loop described above.
4. **Document recovery as automatic** in `k8s/agents/tana-mcp/README.md`,
   with the bootstrap-via-extractor flow as the only manual procedure.

Options A and C remain useful as fallbacks if either piece of B turns out
to be unstable across Tana versions. Option D stays the escape hatch for
the day Tana actively breaks the custom-token sign-in surface.

## Open questions

- Does Tana invalidate the Firebase refresh token on its own cadence, or only
  reactively (password change, "sign out of all sessions", revoked client)?
  Determines how often the option-B sidecar actually has to act, and whether
  the SOPS refresh token needs occasional manual rotation.
- What's the exact Cloud Function URL for `fetchCustomToken`? The web bundle
  uses Firebase callable wiring (`Ht(t, "fetchCustomToken")` over
  `europe-west1`). Resolve that to the concrete URL when writing the sidecar
  rather than scraping it at runtime.
- For option A's xdg-mime variant: does the in-image `xdg-utils` +
  `epiphany-browser` install have any host-side side effect? It shouldn't,
  but worth confirming before documenting the host handler swap as an
  option A path.
