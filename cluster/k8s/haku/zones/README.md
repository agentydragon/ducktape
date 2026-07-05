# Haku worker zones

A **zone** is the concrete per-provider infrastructure track a worker runs in: a
namespace + its egress perimeter + the harness + the Job templates that run there. Trust
level is a property of the zone — the zone's perimeter is what enforces what may enter a
worker's context, never agent restraint. Jobs are dispatched _to a zone_.

Design + trust model + the not-yet-built zones: <../../../../haku/plans/multi_agent.md>.
The dispatcher that stamps Jobs into these namespaces:
<../dispatch/README.md>. Security contract: <../../../../haku/docs/security.md>.

## Live zones

| Zone  | Namespace          | Provider | Admits in context           | Harness         |
| ----- | ------------------ | -------- | --------------------------- | --------------- |
| `zai` | `haku-sandbox-zai` | z.ai GLM | public-by-construction only | Claude Code CLI |

`oai` (`haku-sandbox-oai`, OpenAI, Codex CLI, moderate curated context) is planned — see
the plan's build order.

## Perimeter

Each zone namespace (`zai/`) is a stamped, **tighter** copy of the `haku-sandbox`
perimeter: namespace + ResourceQuota + LimitRange + a **no-grants worker ServiceAccount**
(`automountServiceAccountToken: false`, no k8s API rights — a hijacked worker can read
nothing via the API, not even its own zone's secrets, only what its pod mounts).

Egress is forced through the shared **`haku-zones-mitmproxy`**
(`cluster/k8s/agents/haku-zones-mitmproxy/`) — the `haku-egress-proxy` pattern minus the
Google FQDNs, with a clusterwide policy carrying **no `toEntities: cluster` and no
kube-apiserver** (much tighter than `haku-sandbox`'s fence). The proxy's own egress CNP is
the FQDN allowlist: v1 = git hosting (`github.com` + friends) + package indexes
(`pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`). No Google, no direct LLM
provider (the LLM path is in-cluster only, via the workers-LiteLLM), no image registries
(the kubelet pulls at the node). Widen only with evidence — each addition is a reviewable
one-line diff in that CNP.

`policies/` holds the Kyverno `ClusterPolicy` that injects the mitmproxy env + trust
bundle into every pod in the zone namespaces (CREATE-only, autogen off — see the policy's
own comment for why). Add each new zone to every rule's namespace list.

## Invariant

Workers never touch `haku-state` in either direction: git write implies read, so the
moment a worker could commit to haku-state it could read the whole personal-data
motherlode. No credential in any zone namespace opens that channel, and no code path
clones it.
