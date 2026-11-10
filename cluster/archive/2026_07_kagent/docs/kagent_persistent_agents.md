# Persistent AI Agents Platform Plan

> **Archived (2026-07-21); suspended since 2026-05-08.** This was an exploratory
> design for persistent agents, not a deployed platform. The [retirement
> record](../README.md) governs any revival: re-evaluate upstream, bound tool
> output **before history ingestion**, and pass an end-to-end noisy-MCP test.
> A single large MCP result exceeded the z.ai coding-plan prompt cap (error
> `1261`); interval compaction between user turns did not protect that turn.
> See [operational findings](operational_findings.md).
>
> These artifacts are not reconciled by Flux. Vault was decommissioned
> 2026-04-19; the proposed `vault-backend` credential recipe is obsolete. Any
> revival must use the current [secret ownership](../../../docs/decisions.md)
> rather than restore these manifests directly.

## Design and artifact owners

The goal was an agent with its own credentials, persistent workspace and isolated
compute, able to run long tasks and optionally operate a desktop. Start with
Kagent's built-in per-agent/session UI; LibreChat (OIDC, more features and models)
and Open WebUI were alternatives.

The selected topology was:

```text
Chat UI → Kagent Agent → per-agent RemoteMCPServer → desktop Service :8080
                                                     ↓
                                Pod: MCP sidecar ↔ Xfce/TigerVNC desktop
                                     localhost X11 :0; VNC :5900
                                                     ↓
                                         workspace + config PVCs
```

The Agent runtime is separate from the desktop Pod. Desktop and MCP containers
share a network namespace; restarting only MCP need not kill desktop processes.
A Pod restart loses processes, open windows and in-memory state, including running
compilations/downloads. PVCs preserve files; the agent must detect interrupted
tasks and relaunch applications. A StatefulSet would provide identity and ordered
rollouts, **not** restore RAM or a visual session; that needs checkpointing such
as CRIU.

The preserved [DevBot directory](../k8s/devbot/) owns the concrete prototype:

| Artifact                                                                                                                                                              | Recorded responsibility                                                                                                                                                                 |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Agent](../k8s/devbot/agent.yaml) and [ModelConfig](../k8s/devbot/modelconfig.yaml)                                                                                   | Declarative agent, system message, model and per-agent tool reference; embedded deployment settings configure the agent runtime, not the desktop.                                       |
| [Deployment](../k8s/devbot/deployment.yaml)                                                                                                                           | One desktop plus MCP sidecar; workspace/config mounted under `/home/devbot/`; X11 `:0` / `localhost:0`.                                                                                 |
| [Service](../k8s/devbot/service.yaml) and [RemoteMCPServer](../k8s/devbot/remotemcpserver.yaml)                                                                       | VNC `5900`, MCP `8080`; `STREAMABLE_HTTP` URL `http://devbot-desktop.agents.svc.cluster.local:8080`. Each agent was to have its own private instance.                                   |
| [PVCs](../k8s/devbot/pvcs.yaml)                                                                                                                                       | Workspace `20Gi`, config `5Gi`, `ReadWriteOnce`, `lvm-proxmox-hdd`.                                                                                                                     |
| [Kustomization](../k8s/devbot/kustomization.yaml)                                                                                                                     | Namespace, PVCs, Deployment, Service, RemoteMCPServer, Agent and ModelConfig. It contains no ExternalSecret.                                                                            |
| [Desktop image](../k8s/devbot/docker/desktop/Dockerfile), [entrypoint](../k8s/devbot/docker/desktop/entrypoint.sh), [xstartup](../k8s/devbot/docker/desktop/xstartup) | Ubuntu 26.04, Xfce, TigerVNC, `devbot` user; default 1920×1080 display. Entrypoint runs VNC in foreground with `VncAuth`, listens beyond localhost and falls back to password `devbot`. |
| [MCP image](../k8s/devbot/docker/mcp-server/Dockerfile)                                                                                                               | Python 3.14, X11 client libraries, OCR dependencies and an unpinned `computer-control-mcp` install; explicit HTTP startup.                                                              |
| [Build script](../k8s/devbot/docker/build.sh)                                                                                                                         | Defaults to local image builds; optional registry/push settings, including the historical Harbor destination.                                                                           |

These files are evidence of the experiment, not validated deployment recipes.
For example, the Service selects `app`/`component` labels while the Deployment
uses `app.kubernetes.io/name`/`app.kubernetes.io/component`; the selectors do not
match. The original inline sketches also differed: Ubuntu 22.04/Python 3.12,
`/home/agent` paths, a copied-source MCP installation and an init container writing
a Git credential store. Those sketches did not establish that the archived files
implemented or tested those choices.

### Computer control and transport

The plan selected [AB498/computer-control-mcp](https://github.com/AB498/computer-control-mcp)
for Python containerization, MIT licensing, cross-platform support, mouse/keyboard,
screen capture, OCR and window management. It preferred its screenshot handling
to tanob's reported 1 MB limit. These were selection-time observations.

Intended operations included screenshot, mouse click/move/down/up/drag, typing,
key press/down/up, OCR with coordinates, screen size and window management. The
old Agent sketch's tool names differed from the MCP inventory; discovery and
end-to-end tests must establish the actual API before relying on either list.
The archived Agent requests all tools with an empty `toolNames` list.

The CLI default was stdio, unsuitable for a remote network endpoint. The chosen
Kagent transport was `STREAMABLE_HTTP` (SSE was another supported option in the
plan). The early RemoteMCPServer sketch proposed a `30s` timeout; the archived CR sets
no explicit timeout. The MCP image explicitly uses:

```python
from computer_control_mcp.core import mcp

mcp.run(transport="http", host="0.0.0.0", port=8080)
```

The recorded Kagent tool types were `McpServer` and `Agent`: other agents could be
tools, while shell execution, HTTP/REST, Python functions and OpenAPI capabilities
needed MCP wrappers. This describes the evaluated version, not current upstream.

### Credentials proposal

The proposed per-agent credentials were Anthropic, Gitea, Harbor and VNC secrets.
The obsolete ESO sketch used `vault-backend`, path `secret/agents/devbot` and a
one-year refresh interval (`8760h`) to avoid rotation issues. A phase checklist
instead named `kv/agents/devbot`; neither is a live secret-owner instruction.
The Git init-container sketch would write Git identity and a mode-0600 credential
store on persistent storage. That init container and the claimed ExternalSecret
are absent from the archived Kustomization/Deployment.

The actual Agent references `devbot-credentials` for Harbor; the desktop references
it for VNC. ModelConfig instead names `kagent-anthropic`. Credential injection,
rotation, permissions and persistence remained validation work.

## Proposed implementation and acceptance

The plan recorded local computer-control verification on Pop!\_OS with `nix-shell`
(screenshot, mouse, keyboard, OCR). Historical checkout `/code/github.com/AB498/computer-control-mcp`
had `shell.nix`, `RUN_ME.sh` and `simple_mcp_test.py`. Manifests and Dockerfiles were
created; this was not cluster end-to-end proof. The old “ready for build and
deployment” claim did not supersede the later retirement decision.

The proposed bring-up order was image build → credentials → deployment →
Pod/container logs → X11/VNC/MCP checks → RemoteMCPServer/controller status →
Agent/UI end-to-end checks. Old steps to unsuspend Flux, import images with
`talosctl` and populate Vault are historical intentions, not a revival procedure.

| Phase           | Work and required evidence                                                                                                                                                                                                                                                                                                 |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CLI prototype   | A shell container and exec-based MCP exposing `run_command`, `read_file`, `write_file`, `list_directory`. Prove persistent execution, clone/build/commit, functional injected credentials and workspace survival across restart before adding X11/VNC.                                                                     |
| Visual agent    | Desktop plus MCP sidecar; prove startup, X11 connectivity, VNC observation, MCP response on `8080`, RemoteMCPServer/Agent readiness and credentials. Chat → screenshot → click `(500, 300)` observed via VNC, then typing/navigation and the observe → decide → act loop. Recheck workspace persistence after Pod restart. |
| Multiple agents | Template the deployment, run 2–3 agents for different purposes, validate concurrent operation and separate networks, credentials and workspaces. Require per-agent deployment in under five minutes.                                                                                                                       |
| Production      | Prometheus metrics/alerts, Loki logs, network policy/RBAC, resource quotas, workspace backup/restore and operational runbooks.                                                                                                                                                                                             |

### Unbuilt Helm chart

`charts/kagent-agent/` was a proposal, not an implemented chart. It would template
Agent, MCP/desktop workloads and Services, PVCs and credentials, with values for
name/namespace, system message/model, desktop enablement, CLI versus visual MCP,
images, workspace/config sizes, credential enablement/backend paths, and per-role
resource requests/limits. The sample install commands described a visual DevBot and CLI DataBot.

The proposed template tree listed separate MCP and desktop Deployments/Services,
contradicting the selected sidecar topology. Resolve that design before implementing
a chart. Likewise, the old estimate of three Pods and roughly 3.75 CPU/7.5 GiB per
visual agent (1.75 CPU/3.5 GiB CLI; five-agent cluster limit) was an unvalidated
capacity estimate inconsistent with the chosen two-container desktop Pod. Size
from measured workloads and the actual runtime/resource definitions.

## Deferred alternatives and triggers

- **CrewAI:** after proving one agent, investigate roles, task delegation,
  hierarchical crews and shared context/memory for research or planner/implementer/
  tester workflows. Historical integration reference:
  [kagent-crewai](https://github.com/kagent-dev/kagent/tree/main/python/packages/kagent-crewai).
- **OpenTelemetry:** after production deployment, export agent → LLM → tool traces
  through a Collector to Jaeger/Tempo and Grafana, alongside Prometheus/Loki, to
  diagnose tool/LLM latency and multi-agent coordination.
- **Agent Sandbox:** after the basic design stabilizes, evaluate stronger isolation,
  pre-warmed pools and cold starts using [agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox).
- **StatefulSet / checkpointing:** stable identity and ordered rollout may help
  operations; process/session preservation requires a separate checkpoint design.
- **Proxmox VMs:** if Pod isolation is insufficient, use Terraform-managed desktops
  with the Kubernetes MCP service connecting to them; account for higher resource
  cost and slower provisioning.
- **Guacamole + Authentik RAC:** browser-based desktop monitoring/debugging when a
  VNC client is insufficient. **Rook-Ceph/RWX:** only if multiple Pods need a shared
  filesystem. Both were linked to the broader cluster `docs/plan.md`.

## Historical references

- Local checkouts: `/code/github.com/kagent-dev/kagent`,
  `/code/github.com/mayflower/vnc-use`, `/code/github.com/AB498/computer-control-mcp`.
- [Kagent documentation](https://kagent.dev/docs),
  [MCP specification](https://modelcontextprotocol.io).
- Anthropic quickstarts were a TODO reference, not evaluated evidence.
