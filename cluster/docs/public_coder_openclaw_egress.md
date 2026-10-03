# Public coder OpenClaw: Agentplane egress cutover

Only the OpenClaw Deployment migrates. The KubeVirt devbox, its PVCs, Iron credentials,
proxy aliases, and Iron trust bundle remain in service. OpenClaw's state and diagnostics
PVCs also remain; do not reset its Matrix session or rotate the Matrix account.

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
   remain healthy. Through approval-gated Agentplane actions, check authenticated GitHub,
   Haku diagnostics, `SELECT 1` on ClickHouse, AIQuota and Brave; report status only, never
   credential-bearing response bodies. Confirm Matrix sync/message health using its
   existing cached session. Exercise fresh login separately only if needed, without
   printing or persisting the returned session token.
4. Verify direct public, ClickHouse and Iron egress fails; direct access to the gateway
   without the relay's token must fail authentication. Confirm the relay's projected-token
   mount is absent from OpenClaw and its init container. No token needs to be read.

Tell the agent after rollout: use the existing environment variables rather than old
`proxy-...` literals. Persistent private scripts were not scanned as part of this change;
if needed, perform an approved scan that reports filenames/matching placeholder names
only, never file contents. Do not globally replace placeholders: the devbox intentionally
continues using Iron values. Generated config is re-seeded automatically; preserve the
rest of the OpenClaw PVC, including Matrix state.

For rollback, revert the Pod cutover and restore the OpenClaw-to-Iron network hop on
both sides, leaving preparation in place. Iron's service, credentials and trust never
left. Reverting the cutover does not require PVC deletion or a devbox restart.
