# The personal-data agent: how we would build it today

Requirements: <../../docs/personal_agents/requirements.md>
§ "Agent: personal-data agent(s)" —
H1 self-hosted runtime, H2 LLM-level rollouts, H3 network-isolated command execution,
H4 durable memory, W2 (want) execution off the harness container.

Nothing here is deployed. It is the shape the `public-coder-agent` work argues for,
with the parts that transfer marked as tested and the parts that do not marked as
open. Where a claim carries an `F` number it was measured; where it does not, it was
not.

## Baseline: the same machine, a narrower fence

The `public-coder-agent` topology transfers whole — OpenClaw as a plain Deployment
(not the operator, F3), Authentik in front, iron-proxy as the sole egress route with a
NetworkPolicy making that unbypassable, cert-manager owning the MITM CA, LiteLLM in
front of the models.

| Requirement              | Status              | Why                                                                                                       |
| ------------------------ | ------------------- | --------------------------------------------------------------------------------------------------------- |
| H1 self-hosted runtime   | met by the baseline | Deployment in-cluster; nothing depends on Claude Code Web                                                 |
| H2 LLM-level rollouts    | met by the baseline | Every completion goes through LiteLLM to Langfuse, full request and response (F20)                        |
| H3 network-isolated exec | met by the baseline | iron-proxy `allowlist` transform, restored from the commented block in the coder's `iron.yaml` (F4, F16)  |
| H4 durable memory        | met by the baseline | PVC-backed workspace plus memory embedding search, working in production (F9)                             |
| W2 exec off the harness  | **not met**         | OpenClaw runs harness and shell in one container; the split-execution path is under-tested (B3, F11, F13) |

H2 is the strongest argument for building this at all: it is precisely what hosted
Haku cannot give you, and it falls out of self-hosting for free.

Three cheap carry-overs that cost a day each if forgotten:

- The `rm -f /state/openclaw.json.last-good /state/openclaw.json.bak*` in the seed
  initContainer, or OpenClaw silently reverts to its own last-good copy of the config
  and your declarative change never takes effect (F19).
- The CA into the **system trust store** by volume mount, plus `NODE_EXTRA_CA_CERTS`
  pointing at the mounted path — not at the initContainer's scratch path, which Node
  ignores silently and then fails with a misleading `SELF_SIGNED_CERT_IN_CHAIN` (F17,
  F18).
- Explicit `resources.requests`, or the namespace LimitRange default applies and the
  gateway is OOMKilled mid-run (recorded in the retired `oc-iron` lab run).

## What does not transfer: the credential model

The coder agent's credential is a static PAT, and `replace` mode handles it in four
lines of YAML. Google is OAuth 2.0: a long-lived refresh token exchanged at a token
endpoint for an access token that expires in an hour. Something has to make that POST.

**Correction to what I said earlier in this thread:** I claimed a substituting proxy
cannot perform the token exchange, and pointed at Airlock as the only way to own it.
That is wrong for iron-proxy. It ships an `oauth_token` transform whose `refresh_token`
grant (RFC 6749) does exactly this — exchange, cache, and refresh before expiry, in
process — and the worked example in its own shipped config is a Gmail one:

```yaml
- name: oauth_token
  config:
    tokens:
      - grant: refresh_token
        require: true # 502 on minting failure, rather than forwarding unauthenticated
        refresh_token: { type: env, var: GOOGLE_REFRESH_TOKEN }
        client_id: { type: env, var: GOOGLE_CLIENT_ID }
        client_secret: { type: env, var: GOOGLE_CLIENT_SECRET }
        token_endpoint: "https://oauth2.googleapis.com/token"
        scopes: ["https://www.googleapis.com/auth/gmail.readonly"]
        rules:
          - host: "gmail.googleapis.com"
```

Two properties worth naming. The access token is minted in memory and never becomes a
Kubernetes object, so there is nothing to mirror, rotate, or leak at rest. And setting
`token_endpoint` also **stubs** that endpoint, so a Google SDK inside the agent can run
its own token dance against a placeholder and still end up authenticated — the agent's
libraries do not need to know they are being brokered.

## The four options, and why the bespoke proxy is dominated

| Option                                    | Who holds the refresh token | Where the access token lives              | Granularity                              |
| ----------------------------------------- | --------------------------- | ----------------------------------------- | ---------------------------------------- |
| **A. MCP server holds the credential**    | the MCP server              | that server's memory                      | per **tool**, plus approval queue        |
| **B. iron-proxy `oauth_token`**           | iron-proxy (from a Secret)  | iron-proxy's memory                       | per host, and per path if it works       |
| **C. Airlock brokers, agent reads token** | Airlock                     | a Secret, ESO-mirrored **into the agent** | none — the agent holds a live credential |
| **D. Bespoke Google proxy**               | your code                   | your code                                 | whatever you write                       |

**D is dominated by B**: it is B with the maintenance. The one thing a bespoke proxy
buys is response-level policy — redacting message bodies, capping a Drive listing,
enforcing a per-day read budget — which neither A nor B does. If that is what you
actually want, write it as a policy layer, not as a credential holder, and let
iron-proxy keep the credential.

**C is what the cluster does today, and it is the weakest of the four.**
`cluster/k8s/agents/airlock/google-access-token-eso.yaml` mirrors a live Google access
token into `claude-sandbox` and `haku-sandbox` on a 1-minute refresh. That is exactly
the exposure the coder agent's design was built to remove: a credential readable from
inside the agent, and therefore reachable by prompt injection from anything the agent
reads. It should not be extended to the personal-data agent.

**A is the strongest where it exists.** `haku/console/tools/{gmail,google_calendar}.py`
already run this way — the console holds the OAuth clients and each Operator's refresh
token, refreshes in process, and every call goes through the operator-approval queue.
That is finer-grained than any proxy can be: `create_event` stays operator-approved
while reads are auto-approved, which is a distinction no host-or-path rule can express.
Its limit is coverage — it only reaches surfaces someone wrote tools for.

**So: A where a tool exists, B for the long tail.** Airlock's role narrows to what it
is actually for — running the human consent flow and holding the refresh token — and
it stops being in the request path at all.

## The ConfigMap-churn worry does not apply

It was the right thing to worry about and it does not bite, because the credential is
never in the ConfigMap in any of these designs. The ConfigMap holds a _reference_
(`{type: env, var: GOOGLE_REFRESH_TOKEN}`); the value arrives by `secretKeyRef`. What
rotates is a Secret, and no manifest changes when it does.

There is a smaller, real version of the problem. iron-proxy's `env` source is read once
when the pipeline is built, so a rotated refresh token needs a proxy restart. Its
sources that re-fetch on a `ttl` are AWS Secrets Manager, AWS SSM, and 1Password
(service account or Connect) — we run none of them, and there is no "file on disk with
a ttl" source, so a mounted Secret does not self-refresh either. The proxy Deployment
already carries `reloader.stakater.com/auto: "true"`, which restarts it on Secret
change; restarting the proxy drops in-flight connections but does not touch the agent's
session, unlike restarting the agent (F14).

How often that fires is the part that matters, and for Gmail it is weekly. Google keeps
an app requesting restricted scopes in **Testing** publishing status unless it goes
through the verification and security-assessment path, and a Testing-status app's
refresh token expires every 7 days — documented in `cluster/k8s/haku/console/README.md`
from the console's own experience with project `rai-personal`. So a Gmail grant needs
reauthorization roughly weekly **regardless of which option above you choose**. That is
a human-in-the-loop event, not an automation gap, and it is the single best argument for
Airlock keeping custody: it is the thing with a browser flow and an Authentik login in
front of it.

## Scope is the fence; the proxy is not

The strongest control here is not any of the above. A refresh token carries a fixed
grant, and if `gmail.modify` is inside it then the only thing standing between the agent
and sending mail is a path rule in a proxy — which is a much weaker guarantee than
Google refusing the call. So:

- **One OAuth client per surface, minimum scopes.** haku-console already does this,
  with separate Google Cloud projects for Mail and Calendar precisely so their
  verification and credential lifecycles do not contaminate each other.
- **Do not reuse Airlock's existing `google` provider.** Its grant is the union of nine
  read-only scopes across Gmail, Drive, Calendar, Tasks, Contacts, Docs, Sheets, Slides
  and YouTube. Add a provider entry per surface the personal-data agent actually needs.
- The proxy's `rules` are then defence in depth over a grant that is already narrow,
  which is the same relationship the coder agent has with GitHub's server-side RBAC.

## Open questions — test these before building on them

1. **Host rules may not separate Calendar from Drive.** Both are reachable under
   `www.googleapis.com` (`/calendar/v3`, `/drive/v3`) as well as under
   `calendar.googleapis.com` / `drive.googleapis.com`, and which one a client library
   picks varies. If the SDK uses `www.googleapis.com`, host-level rules cannot tell the
   two apart and you need `paths:`. Untested.
2. **Whether `paths:` on an `oauth_token` rule needs a paired CONNECT rule.** The
   CONNECT-pairing trap in F15/F16 was in the **allowlist** transform, where a rule
   decides whether a request is permitted and the header-less CONNECT preflight matches
   none of the scoped rules. An `oauth_token` rule only selects which requests get a
   token, so it should not have the problem — but that is reasoning, not a measurement,
   and the same reasoning would have told you `require: true` was safe.
3. **Whether Airlock and iron-proxy can both hold the same refresh token.** Google does
   not rotate refresh tokens on use, so both should be able to read the same value
   without one invalidating the other. If it turns out to rotate, iron-proxy's copy goes
   stale between Secret update and reloader restart. Cheap to test, and the fallback is
   a separate OAuth client for the proxy.
4. **`require: true` on `oauth_token`.** It is documented as a per-entry flag with 502
   semantics, distinct from the `secrets` transform's flag that broke every HTTPS
   request in tunnel mode (F15). Verify it against a real CONNECT before trusting it.
5. **Whether MCP servers should traverse the egress proxy at all.** They hold their own
   credentials and call Google directly, so their traffic never passes the agent's
   fence. That is fine when the MCP server is a separate pod with its own policy, and
   quietly wrong if it is in-process in the harness.

## What changes about the threat model

For the coder agent the asset was the GitHub token, and the allowlist was defence in
depth around a credential that GitHub's own RBAC already constrained. Here the asset is
the personal data itself, sitting in the harness's context after the first tool call. An
allowlist that stops the agent reaching `evil.example` does not stop it writing to an
allowlisted Google Doc, and no credential proxy helps once the data is in the context
window.

That is what makes **W2** worth more here than it was for the coder. Splitting execution
off the harness container would at least keep tool output out of the process that also
holds the model conversation. It remains blocked on the same thing: OpenClaw's
split-execution path syncs on `exec` return, so a command outrunning `yieldMs` is
snapshotted mid-write and the next `exec` restores the partial state over it (B3). Until
that is fixed upstream or we move harnesses, one container is what we have.
