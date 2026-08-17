# Artifact drafts: Haku on a provider-agnostic loop (Runtime C)

Status: **sketches**, not landed code. Companion to
[runtime_options.md](runtime_options.md) (Runtime C); pairs with
[managed_agents_artifacts.md](managed_agents_artifacts.md) (Runtime B) so both
runtimes are equally concrete before choosing. Framework shown: **Pydantic AI**
on the in-cluster **LiteLLM**. Graduates into a real `haku/runtime/agent/` component once
chosen. Kwarg/import names marked _(verify)_ move between Pydantic AI 1.x
releases — pin a version and check.

**One repo-side path in these sketches has since moved.** They bake Haku's manual and run
procedure out of ducktape (`COPY haku/base`, `COPY haku/run.md`, and the system prompt pointing at
`/opt/haku/base/instructions.md`). #3951 deleted both: the manual lives in the `haku-state` repo
now, which this runtime already clones to `/workspace/haku-state`, so the image bakes nothing and
the prompt points there instead. `haku/base/` still exists but holds only `agent_shared.yaml`.

**Shape vs Runtime B:** there is no Anthropic-run loop and no separate worker —
the agent loop runs in **your** process, so B's "worker + supervisor + session"
collapse into **one** in-cluster service. MCP auth is in-process (pass the bearer
header straight to the client; no vault, no public-facade requirement — the
in-cluster loop can reach in-cluster MCP servers directly). The model goes
through LiteLLM, so the provider is a config knob and only LiteLLM holds provider
keys.

## 1. The agent (`haku_agent.py`)

```python
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIModel        # (verify import path across 1.x)
from pydantic_ai.providers.openai import OpenAIProvider


# Model layer: route through in-cluster LiteLLM (OpenAI-compatible /v1). The model
# string is whatever LiteLLM registers (an alias or "provider/model"); swapping it
# picks the provider. Only LiteLLM holds Anthropic/OpenAI/Z.AI keys.
def litellm_model(name: str) -> OpenAIModel:
    return OpenAIModel(
        name,
        provider=OpenAIProvider(
            base_url="http://litellm.litellm.svc.cluster.local/v1",  # (verify Service)
            api_key=os.environ["LITELLM_API_KEY"],                   # Haku's scoped virtual key
        ),
    )


model = FallbackModel(
    litellm_model("anthropic/claude-opus-4-8"),
    litellm_model("zai/glm-4.6"),
)


@dataclass
class Deps:
    state_repo: Path
    http: httpx.AsyncClient


haku_console = MCPServerStreamableHTTP(
    "https://haku.allegedly.works/mcp",                    # haku-console's aggregated catalog
    headers={"Authorization": f"Bearer {os.environ['HAKU_CONSOLE_TOKEN']}"},
)

haku = Agent(
    model,
    deps_type=Deps,
    toolsets=[haku_console],    # (verify: toolsets= vs mcp_servers= for your version)
    instrument=True,            # native OTel → Langfuse (configure OTLP exporter via env)
    system_prompt=(
        "You are Haku, the operator's background executive assistant. Your manual "
        "is at /opt/haku/base/instructions.md and your run procedure at "
        "/opt/haku/run.md; your haku-state checkout (your only memory and write "
        "surface) is at /workspace/haku-state, kubeconfig and git auth in place. "
        "Read the manual and the run procedure, then execute one scan pass."
    ),
)


@haku.tool
async def run_command(ctx: RunContext[Deps], command: str) -> str:
    """Run a shell command (kubectl/psql/git/curl/fastmcp). The Pod is the trust
    boundary, so commands run unsandboxed; everything reachable is read-only."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=ctx.deps.state_repo,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return out.decode()[-20000:]    # tail, to keep tool results bounded
```

`bash` + the Tana MCP toolset cover Haku's surface today; add more MCP toolsets
(grocy, postscanmail, google) the same way, or keep reaching them via `bash`/curl
as now. Note this is the C advantage over B: no vault and no public-facade
requirement — an in-cluster loop can call an in-cluster MCP Service directly.

## 2. The supervisor / wake endpoint (`supervisor.py`)

No session to keep alive or reconnect (the loop is in-process), so "wake" is just
`agent.run()` under a lock.

```python
import asyncio
import contextlib
from pathlib import Path

import httpx
from fastapi import FastAPI

from haku_agent import Deps, haku

STATE = Path("/workspace/haku-state")
app = FastAPI()
_lock = asyncio.Lock()
WAKE = "Wake: do one scan pass per the run procedure, then commit, push, and stop."


async def wake(text: str = WAKE) -> None:
    async with _lock:                       # serialize: one scan at a time
        async with httpx.AsyncClient() as http, haku:   # `async with haku` opens MCP conns (verify)
            await haku.run(text, deps=Deps(state_repo=STATE, http=http))
    # haku-state git IS the durable memory — nothing else to persist. To keep a
    # warm conversation instead, store result.all_messages() and pass it as
    # message_history= on the next wake.


@app.post("/wake")
async def http_wake() -> dict[str, str]:
    asyncio.create_task(wake())             # ack fast; run in the background
    return {"status": "woken"}


async def _scheduler(period_s: float = 3600.0) -> None:
    while True:
        await asyncio.sleep(period_s)
        await wake()


@app.on_event("startup")
async def _startup() -> None:
    app.state.sched = asyncio.create_task(_scheduler())


@app.on_event("shutdown")
async def _shutdown() -> None:
    app.state.sched.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await app.state.sched
```

Crash-survivability here is "re-orient from `haku-state` on restart" (cheap, since
the scan is incremental via bookmarks). If you want true checkpointed resume of an
in-flight run, that's the point where **Dapr Agents** or a Pydantic AI
durable-execution integration (DBOS/Temporal) would slot in instead of this
hand-rolled loop.

## 3. Container + entrypoint

Runtime B's image minus the Anthropic worker: bake base, reuse `bootstrap.sh`
(kubeconfig + clone state), then run uvicorn.

```dockerfile
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates postgresql-client bash \
 && rm -rf /var/lib/apt/lists/*
COPY --from=bitnami/kubectl:latest /opt/bitnami/kubectl/bin/kubectl /usr/local/bin/kubectl
RUN pip install --no-cache-dir pydantic-ai fastapi uvicorn httpx fastmcp
COPY haku/base /opt/haku/base
COPY haku/run.md /opt/haku/run.md
COPY devinfra/k8s/kubeconfig.py /opt/haku/kubeconfig.py
COPY haku/runtime/agent/entrypoint.sh /opt/haku/entrypoint.sh
COPY haku/runtime/agent/haku_agent.py /opt/haku/haku_agent.py
COPY haku/runtime/agent/supervisor.py /opt/haku/supervisor.py
USER 1000
WORKDIR /workspace
ENTRYPOINT ["/opt/haku/entrypoint.sh"]
```

`entrypoint.sh` is the same bootstrap as Runtime B (materialize kubeconfig from
the JWT, write `~/.netrc`, clone `haku-state` to `/workspace/haku-state`), then:

```bash
exec uvicorn supervisor:app --host 0.0.0.0 --port 8080
```

## 4. k8s wiring (`cluster/k8s/haku/`)

A single `haku-agent` Deployment (no worker/supervisor split), in `haku-sandbox`,
non-root, behind `haku-egress-proxy`, scoped RBAC — same perimeter as today.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: haku-agent
  namespace: haku-sandbox
spec:
  replicas: 1
  selector: { matchLabels: { app: haku-agent } }
  template:
    metadata: { labels: { app: haku-agent } }
    spec:
      serviceAccountName: haku
      securityContext: { runAsNonRoot: true, runAsUser: 1000 }
      containers:
        - name: agent
          image: ghcr.io/agentydragon/haku-agent # Flux image automation pins the tag
          env:
            - { name: K8S_JWT_SOPS_PATH, value: secrets/haku-k8s-jwt.yaml }
            - { name: K8S_USER, value: haku }
            - { name: K8S_NAMESPACE, value: haku-sandbox }
            - name: LITELLM_API_KEY
              valueFrom: { secretKeyRef: { name: haku-litellm-key, key: key } }
            - name: HAKU_CONSOLE_TOKEN
              valueFrom: { secretKeyRef: { name: haku-console-agent-api, key: token } }
            # OTEL_EXPORTER_OTLP_* → Langfuse; provider keys live in LiteLLM, not here.
```

Plus a `Service` and a Forgejo webhook on `haku-state` → `POST /wake`.

## What's different from Runtime B

- **One process, not three** — no Anthropic loop, no worker, no session supervisor.
- **MCP auth in-process** — bearer header straight to the client; no vault, no
  public-facade requirement; in-cluster MCP Services reachable directly.
- **Provider is a config knob** (LiteLLM); attribution + traces via
  LiteLLM + Langfuse (the keys never leave LiteLLM).
- **No Console** — observe in Langfuse.
- **Crash-resume** = re-orient from `haku-state` (or adopt Dapr/durable-exec for
  true checkpointing).

## Open questions / verify

- Pydantic AI 1.x specifics: `toolsets=` vs `mcp_servers=`, the MCP lifecycle
  context manager (`async with haku` vs `run_mcp_servers()`), and the
  `OpenAIModel`/`OpenAIProvider` import paths. Pin a version.
- The in-cluster LiteLLM Service DNS, and that Haku's virtual key is registered
  for the model strings used (`anthropic/...`, `zai/glm-...`).
- Warm `message_history` (cost: re-send history each wake, mostly cache-read) vs.
  stateless re-orient (cost: re-read `haku-state` each wake; cheap — git is
  incremental via bookmarks). Default to stateless; git is the memory.
- Read-only stays structural (mitmproxy egress + read-only creds + scoped RBAC) —
  unchanged from today; the loop owning provider keys doesn't widen Haku's reach.
