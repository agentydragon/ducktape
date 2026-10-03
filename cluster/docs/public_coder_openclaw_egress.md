# Public coder OpenClaw: Agentplane egress cutover

Only the OpenClaw Deployment migrates. The KubeVirt devbox, its PVCs, Iron credentials,
proxy aliases, and Iron trust bundle remain in service. OpenClaw's state and diagnostics
PVCs also remain; do not reset its Matrix session or rotate the Matrix account.

## Handoff — 2026-10-03

**The proxy cutover is deployed; end-to-end acceptance is not complete.** Track the
remaining checks in [#8857](https://github.com/agentydragon/ducktape/issues/8857).
Preparation [#8849](https://github.com/agentydragon/ducktape/pull/8849) and cutover
[#8850](https://github.com/agentydragon/ducktape/pull/8850) are merged.

Dated observations from the rollout, not ongoing monitoring:

- Preparation: staging gateway 2/2 Ready, all four new credential copies
  Ready/SecretSynced, and the public CA ConfigMap present in `public-coder-agent`.
- A cutover Pod's relay returned HTTP 200 readiness and the expected policies for
  `public-coder-agent/openclaw`. Its projected egress token was mounted only in the
  relay, not in OpenClaw or the init container.
- Latest approved check: OpenClaw Pod 2/2 Ready with zero restarts; app Flux
  Ready/Healthy at origin `82f07209ecaccddc16735f78a8134befdcec0476`
  (health transition 2026-10-03 08:23:50 UTC). Iron proxy was 1/1 Ready and the
  current devbox launcher 3/3 Ready. Neither workload was frozen against later rolls.
- Authenticated service and negative-egress probes have **not** been verified:
  earlier attempts could not execute because the target Pod had been replaced;
  the replacement diagnostic requests were withdrawn and confirmed cancelled when
  maintenance was deferred. Cancellation is not a passing test.
- The operator reported an agent database at schema version 19 requiring
  `openclaw doctor --fix` to migrate session identities, with sessions unavailable.
  **Pod readiness does not establish session or Matrix health.** No database repair,
  manual Gateway shutdown/restart or data deletion was performed by this verification.

The VM deliberately stays on Iron pending
[#8856](https://github.com/agentydragon/ducktape/issues/8856). Remaining proxy consumers
and retirement sequencing are tracked in
[#5140](https://github.com/agentydragon/ducktape/issues/5140).

### Offline session-database maintenance

This is deferred maintenance, not a step to run against an active Gateway. Confirm
and schedule it with the operator; keep its evidence/checklist in #8857.

1. Identify affected databases and the intended OpenClaw image/version without
   exposing session contents. Confirm the diagnostic against that version.
2. Stop the Gateway and every other writer. Arrange for Flux, image automation and
   other controllers not to restart them during the window; record how normal
   reconciliation will be restored.
3. Take and verify an offline backup, including relevant SQLite/WAL state. Retain
   the matching image/configuration and establish restore steps before mutation.
4. Run `openclaw doctor --fix` with the intended version, inspect its result, then
   restart and restore normal reconciliation. Verify existing sessions/history and
   Matrix before declaring recovery; retain the backup through acceptance.

Do not hand-edit SQL, delete a database/PVC, reset Matrix sessions, or rotate the
account as a shortcut. The older bounded-startup/integrity investigation
[#5646](https://github.com/agentydragon/ducktape/issues/5646) is related but does not
establish that this schema migration has run. Proxy rollback below does **not** undo
a database migration; database recovery needs its own verified backup/restore plan.

## Transport and credentials

OpenClaw uses the standard Agentplane relay at `http://127.0.0.1:3128`. Only the relay
mounts a short-lived projected token, with audience `agentplane-egress`. The dedicated
`public-coder-agent/openclaw` ServiceAccount has no new Kubernetes RBAC grants or
Kubernetes-audience token. Kubernetes calls still use the existing Haku static-Agent
bearer through the Haku kube-api proxy, with unchanged authorization.

The staging gateway admits that exact ServiceAccount through an explicit EgressBinding.
This does **not** grant access to the staging application's operator API or login secret,
and does not change any managed-agent preset or testing-environment binding. Public
Internet access preserves Iron's existing unrestricted public destinations. The app's
network policy still forbids direct Internet, ClickHouse, and Iron connections.

The gateway reads canonical credential sources into its isolated credentials namespace,
not from the app's Iron mirrors. Haku, ClickHouse and Matrix each get an exact-source
Secret reader; Brave reuses the existing external-creds mechanism. GitHub and AIQuota
reuse the gateway's existing credentials. Haku identity and ClickHouse username stay
unchanged. Matrix substitution is only the `password` JSON field on
`POST https://matrix.allegedly.works/_matrix/client/v3/login`.

Existing environment-variable names stay stable:

| Variable                           | Agentplane credential name  |
| ---------------------------------- | --------------------------- |
| `GH_PAT`, `GITHUB_TOKEN`           | `github-pat`                |
| `HAKU_CONSOLE_TOKEN`               | `public-coder-haku-console` |
| `CLICKHOUSE_PUBLIC_CODER_PASSWORD` | `public-coder-clickhouse`   |
| `AIQUOTA_API_BEARER_TOKEN`         | `aiquota-read`              |
| `BRAVE_API_KEY`                    | `brave-search`              |
| `MATRIX_PASSWORD`                  | `public-coder-matrix`       |

The generator supplies canonical placeholders, the Matrix proxy setting, and kubeconfig.
No `gh` re-login is needed: the image's wrapper still exports `GH_TOKEN` from `GH_PAT`.
LiteLLM, local gateway authentication, and SSH credentials are unchanged.

## Rollout and verification

1. Land the gateway preparation first: external namespace admission, credential copies,
   policy/binding, network hops and public CA bundle distribution. Verify staging Flux,
   gateway replicas, all four ExternalSecrets and the app-namespace CA ConfigMap are Ready.
   Do not read Secret values. Preparation alone does not restart OpenClaw.
2. Only then land the Pod cutover. Flux additionally makes the app depend on staging,
   whose health checks include the new credential copies. Dependency readiness is not an
   atomic same-revision deployment barrier; the two merge steps are deliberate.
3. Verify the OpenClaw Pod is 2/2 Ready (Deployment 1/1), its relay is ready, and the VM and Iron
   remain healthy. Check exact effective Kubernetes permissions and Action policy first;
   prefer standing access or a matching auto-approved Action, requesting operator approval
   only where needed. Check authenticated GitHub, Haku diagnostics, `SELECT 1` on ClickHouse,
   AIQuota and Brave through the relay; report status only, never
   credential-bearing response bodies. Confirm Matrix sync/message health using its
   existing cached session. Exercise fresh login separately only if needed, without
   printing or persisting the returned session token.
4. Verify direct public, ClickHouse and Iron egress fails; direct access to the gateway
   without the relay's token must fail authentication. Confirm the relay's projected-token
   mount is absent from OpenClaw and its init container. No token needs to be read.
5. Record evidence in #8857, including ordinary public Internet access, preserved state
   and Matrix sync/message health; update this handoff only after observing those behaviors.
   For each probe, use the current Pod and discover its effective egress rules first.

The validating managed sandbox lacked standing Kubernetes grants. The operator believes
it was launched before grant support existed; that is not evidence of a current binding
controller defect and does not justify broadening RBAC. Check each future sandbox's own
access rather than inheriting that assumption. Shared access/approval/withdrawal guidance
was merged in [#8852](https://github.com/agentydragon/ducktape/pull/8852).

Tell the agent after rollout: use the existing environment variables rather than old
`proxy-...` literals. Persistent private scripts were not scanned as part of this change;
if needed, perform an approved scan that reports filenames/matching placeholder names
only, never file contents. Do not globally replace placeholders: the devbox intentionally
continues using Iron values. Generated config is re-seeded automatically; preserve the
rest of the OpenClaw PVC, including Matrix state.

For rollback, revert the Pod cutover and restore the OpenClaw-to-Iron network hop on
both sides, leaving preparation in place. Iron's service, credentials and trust never
left. Reverting the cutover does not require PVC deletion or a devbox restart.
