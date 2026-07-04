# Artifact drafts: Haku on Managed Agents (self-hosted)

First-cut, copy-pasteable drafts of the artifacts in the
[migration plan](managed_agents.md) (its "Artifacts to build" section).
**Status: partially superseded.** The component landed in
[`haku/runtime/managed_agent/self_hosted/`](../runtime/managed_agent/self_hosted/README.md) using an
**ant-all-the-way** approach (no `anthropic` Python SDK — see that README for
why). The control-plane YAML (§§1–3) and the vault wiring (§3) carry over; the
SDK worker (§4) is replaced by `ant beta:worker poll`, and the Python supervisor
(§5) is **deferred** in favor of a scheduled deployment (`haku.deployment.yaml`).
Field names flagged for verification are not fully pinned in the skill docs —
confirm against `ant <cmd> --help` before relying on them.

The runtime choice itself (A vs B vs C) remains open — see
[runtime_options.md](runtime_options.md). The `self_hosted/` code is an evaluation
prototype of the B path, not a commitment to it.

Naming below assumes: base manual baked into the worker image at `/opt/haku`
(the PLAN's image model — base ships by image rebuild, reconciliation is against
the image's pinned version); `haku-state` cloned at runtime to
`/workspace/haku-state`; agent working dir `/workspace`.

## 1. Environment (`haku.environment.yaml`)

```yaml
name: haku-selfhosted
config:
  type: self_hosted
```

```sh
ENV_ID=$(ant beta:environments create < haku.environment.yaml --transform id -r)
# Generate the environment key in Console (Environments → this env → "Generate
# environment key"); it is the worker's only Anthropic credential.
```

## 2. Agent (`haku.agent.yaml`)

Thin `system` (a pointer to the baked manual — behavior stays single-sourced in
`haku/base/`); the full toolset auto-allowed (the Pod is the trust boundary); one
`mcp_toolset` for `tana-mcp-ro` to start.

```yaml
name: haku
model: claude-opus-4-8
system: |
  You are Haku, the operator's tireless background executive assistant. The
  worker has bootstrapped your home: your operating manual is baked at
  /opt/haku/base/instructions.md and your run procedure at /opt/haku/run.md;
  your haku-state checkout (your only memory and write surface) is at
  /workspace/haku-state, with ~/.kube/config and git auth already in place.
  Read the manual and the run procedure, then execute the run procedure end to
  end. Each user message is a wake: do one scan pass, commit and push your
  state, then stop and wait for the next wake.
tools:
  - type: agent_toolset_20260401
    default_config:
      enabled: true
      permission_policy:
        type: always_allow
  - type: mcp_toolset
    mcp_server_name: tana-ro
mcp_servers:
  - type: url
    name: tana-ro
    url: https://tana-mcp-ro.allegedly.works/mcp
```

```sh
AGENT_ID=$(ant beta:agents create < haku.agent.yaml --transform id -r)
# Iterate later with: ant beta:agents update --agent-id "$AGENT_ID" --version N < haku.agent.yaml
```

## 3. Vault + the `tana-mcp-ro` credential

One vault holds MCP creds; `tana-mcp-ro` is gated by the static bearer Haku
already has reflected into `haku-sandbox` (`haku-tana-ro-token`). The token is
piped via stdin (never on argv).

```sh
VAULT_ID=$(ant beta:vaults create --name haku-mcp --transform id -r)

TANA_TOKEN=$(kubectl -n haku-sandbox get secret haku-tana-ro-token \
  -o jsonpath='{.data.token}' | base64 -d)

# static_bearer credential keyed by the MCP server URL. (verify: exact field
# names — the skill documents mcp_oauth in full but only names static_bearer.)
ant beta:vaults:credentials create --vault-id "$VAULT_ID" <<YAML
display_name: tana-mcp-ro (read-only)
auth:
  type: static_bearer
  mcp_server_url: https://tana-mcp-ro.allegedly.works/mcp
  token: ${TANA_TOKEN}
YAML
```

The vault is attached per session via `vault_ids` (see the supervisor). Upgrading
`tana-mcp-ro` to its Authentik OIDC mode later swaps this for an `mcp_oauth`
credential (auto-refresh), which is what the <../TODO.md> Tana entry wants.

## 4. Worker image

Image contents (becomes a Bazel `oci_image` per
<../../cluster/docs/container-images.md>; shown as a Dockerfile for clarity):

```dockerfile
# Runs in haku-sandbox as non-root, behind haku-egress-proxy egress.
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates postgresql-client bash \
 && rm -rf /var/lib/apt/lists/*
# kubectl + fastmcp (fastmcp is in the agent-haku Nix closure today; pin both)
COPY --from=bitnami/kubectl:latest /opt/bitnami/kubectl/bin/kubectl /usr/local/bin/kubectl
RUN pip install --no-cache-dir anthropic fastmcp
# Baked base (PLAN image model) + the kubeconfig helper + the worker.
COPY haku/base /opt/haku/base
COPY haku/run.md /opt/haku/run.md
COPY devinfra/k8s/kubeconfig.py /opt/haku/kubeconfig.py
COPY haku/managed_agents/entrypoint.sh /opt/haku/entrypoint.sh
COPY haku/managed_agents/worker.py /opt/haku/worker.py
USER 1000
WORKDIR /workspace
ENTRYPOINT ["/opt/haku/entrypoint.sh"]
```

`entrypoint.sh` — the same bootstrap steps as
`haku/runtime/claude_web_env/bootstrap.sh`, retargeted to `/workspace` (factor the shared
logic into one script when this lands):

```bash
#!/usr/bin/env bash
set -euo pipefail
state_dir=/workspace/haku-state
ns=haku-sandbox

# 1. kubeconfig from the haku JWT (K8S_* env set on the Deployment).
python3 /opt/haku/kubeconfig.py --write "$HOME/.kube/config"

# 2. git auth for the state repo, off any command line.
u=$(kubectl -n "$ns" get secret haku-state-git-write -o jsonpath='{.data.username}' | base64 -d)
p=$(kubectl -n "$ns" get secret haku-state-git-write -o jsonpath='{.data.password}' | base64 -d)
umask 077
printf 'machine git.allegedly.works login %s password %s\n' "$u" "$p" >"$HOME/.netrc"

# 3. clone or fast-forward state.
if [ -d "$state_dir/.git" ]; then
  git -C "$state_dir" pull --ff-only || true
else
  git clone https://git.allegedly.works/haku/haku-state.git "$state_dir"
fi
git -C "$state_dir" config user.name haku
git -C "$state_dir" config user.email haku@allegedly.works

exec python3 /opt/haku/worker.py
```

`worker.py` — the always-on self-hosted worker (the loop runs at Anthropic; this
just executes tool calls in the Pod):

```python
import asyncio
import os

from anthropic import AsyncAnthropic
from anthropic.lib.environments import EnvironmentWorker


async def main() -> None:
    environment_key = os.environ["ANTHROPIC_ENVIRONMENT_KEY"]
    environment_id = os.environ["ANTHROPIC_ENVIRONMENT_ID"]
    async with AsyncAnthropic(auth_token=environment_key) as client:
        await EnvironmentWorker(
            client,
            environment_id=environment_id,
            environment_key=environment_key,
            workdir="/workspace",
        ).run()


if __name__ == "__main__":
    asyncio.run(main())
```

## 5. Supervisor (`supervisor.py`)

Owns **exactly one live session**, exposes a wake endpoint (Forgejo webhook +
manual), runs the schedule, and keeps a lossless event stream so it knows when a
session died. Skeleton — the load-bearing control flow, not production-hardened:

```python
import asyncio
import contextlib
import os

from anthropic import AsyncAnthropic
from fastapi import FastAPI

AGENT_ID = os.environ["HAKU_AGENT_ID"]
ENV_ID = os.environ["HAKU_ENVIRONMENT_ID"]
VAULT_ID = os.environ["HAKU_VAULT_ID"]
WAKE = "Wake: do one scan pass per the run procedure, then commit, push, and stop."

client = AsyncAnthropic()  # ANTHROPIC_API_KEY — the control-plane key, worker never sees it
app = FastAPI()
_lock = asyncio.Lock()
_session_id: str | None = None


async def _live(session_id: str | None) -> bool:
    if session_id is None:
        return False
    s = await client.beta.sessions.retrieve(session_id)
    return s.status != "terminated"


async def ensure_session() -> str:
    """Return a live session id, creating one (warm, long-lived) if needed."""
    global _session_id
    async with _lock:
        if not await _live(_session_id):
            session = await client.beta.sessions.create(
                agent=AGENT_ID, environment_id=ENV_ID, vault_ids=[VAULT_ID],
            )
            _session_id = session.id
            asyncio.create_task(_consume(session.id))  # tail its stream until terminal
        return _session_id


async def wake(text: str = WAKE) -> None:
    session_id = await ensure_session()
    await client.beta.sessions.events.send(
        session_id=session_id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": text}]}],
    )


async def _consume(session_id: str) -> None:
    """Lossless tail: history first (dedupe), then live stream; mark dead on terminal."""
    global _session_id
    seen: set[str] = set()
    async with client.beta.sessions.events.stream(session_id=session_id) as stream:
        async for event in client.beta.sessions.events.list(session_id=session_id):
            seen.add(event.id)
        async for event in stream:
            if event.id not in seen:
                seen.add(event.id)
                # TODO: optionally mirror transcript to haku-traces here.
            if event.type == "session.status_terminated":
                break
            if event.type == "session.status_idle" and event.stop_reason.type != "requires_action":
                # idle-and-done — keep the session warm for the next wake; don't break the stream
                continue
    if _session_id == session_id:
        _session_id = None  # next wake recreates; haku-state carries memory forward


@app.post("/wake")
async def http_wake() -> dict[str, str]:
    # Forgejo push webhook + manual trigger land here. (Add HMAC verification.)
    await wake()
    return {"status": "woken"}


async def _scheduler(period_s: float = 3600.0) -> None:
    while True:
        await asyncio.sleep(period_s)
        await wake()


@app.on_event("startup")
async def _startup() -> None:
    app.state.scheduler = asyncio.create_task(_scheduler())


@app.on_event("shutdown")
async def _shutdown() -> None:
    app.state.scheduler.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await app.state.scheduler
```

Notes: a real version needs SSE reconnect-on-drop (re-run the history+stream
overlap), the post-idle status-write race guard before any cleanup, and webhook
HMAC verification — all documented patterns. The idle handler deliberately
**keeps the stream open** so the session stays warm; it only drops `_session_id`
on a true terminal so the next wake recreates and re-orients from `haku-state`.

## 6. k8s wiring (`cluster/k8s/haku/`)

Two Deployments in `haku-sandbox`, secrets split by trust:

- **`haku-worker`** — the worker image; mounts only `ANTHROPIC_ENVIRONMENT_KEY`
  (+ `ANTHROPIC_ENVIRONMENT_ID`) and the `K8S_*` env the existing profile uses to
  materialize the haku JWT kubeconfig. Non-root, behind `haku-egress-proxy`, scoped
  RBAC + quota — unchanged perimeter.
- **`haku-supervisor`** — the supervisor; mounts the org-scoped
  `ANTHROPIC_API_KEY` (**kept off the worker host** so agent tool calls can't read
  it) and `HAKU_{AGENT,ENVIRONMENT,VAULT}_ID`. Exposes `/wake`; a Forgejo webhook
  on `haku-state` and a manual button both POST it.

Worker Deployment, abbreviated:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: haku-worker
  namespace: haku-sandbox
spec:
  replicas: 1
  selector: { matchLabels: { app: haku-worker } }
  template:
    metadata: { labels: { app: haku-worker } }
    spec:
      serviceAccountName: haku
      securityContext: { runAsNonRoot: true, runAsUser: 1000 }
      containers:
        - name: worker
          image: ghcr.io/agentydragon/haku-worker # Flux image automation pins the tag
          env:
            - { name: K8S_JWT_SOPS_PATH, value: secrets/haku-k8s-jwt.yaml }
            - { name: K8S_USER, value: haku }
            - { name: K8S_NAMESPACE, value: haku-sandbox }
            - name: ANTHROPIC_ENVIRONMENT_KEY
              valueFrom: { secretKeyRef: { name: haku-anthropic-env-key, key: key } }
            - name: ANTHROPIC_ENVIRONMENT_ID
              valueFrom: { secretKeyRef: { name: haku-anthropic-env-key, key: environment_id } }
```

## Wire-up order

1. `ant beta:environments create` → `ENV_ID`; generate the environment key.
2. `ant beta:vaults create` + the `tana-mcp-ro` credential → `VAULT_ID`.
3. `ant beta:agents create` → `AGENT_ID`.
4. Build + push the worker image; deploy `haku-worker` (env key + `ENV_ID`).
5. Deploy `haku-supervisor` (API key + the three IDs); point a Forgejo webhook at
   `/wake`.
6. `POST /wake` once by hand → watch the session live in Console
   (`platform.claude.com/workspaces/default/sessions`).
