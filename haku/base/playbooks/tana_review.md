# tana_review (example)

The operator's Tana workspace — daily notes, captured tasks, project nodes — is
full of things they meant to do and never closed out. You can read it (read-only)
through the `tana-mcp-ro` facade, which exposes only Tana's read tools
(`search_nodes`, `read_node`, `get_children`, `open_node`, `list_tags`,
`list_workspaces`, `get_tag_schema`); every write tool is hidden and rejected, and
the Tana PAT stays server-side, so you never see it.

## Reaching it

`tana-mcp-ro` is cluster-internal (`tana-mcp-ro.tana-mcp.svc.cluster.local:8765`),
so your home can't reach it — drive it from a `haku-sandbox` pod the way you query
Plaid: bake the work into the pod's **command** (or a ConfigMap-mounted script) and
read results from `kubectl logs` (`exec`/`port-forward` don't work through the API
gateway). **Verified** from a sandbox pod: `GET /healthz` → `200`, and `POST /mcp`
without the bearer → `401` — the path and the bearer gate both work, you just need
the token. Two things that save time:

- The endpoint is under `.svc.cluster.local`, which your pod's injected `NO_PROXY`
  already covers, so the call goes **direct**, not through the mitmproxy.
- You can't `pip install` in the sandbox (egress allowlist), so use a stock
  `python:3-slim` image with a **stdlib** client — not `fastmcp`.

Mount the bearer from `haku-tana-ro-token` as an env var (`secretKeyRef`, so it
never lands on a command line), then speak Streamable-HTTP MCP: `initialize` (keep
the `Mcp-Session-Id` response header), `notifications/initialized`, then `tools/list`
/ `tools/call` — responses may be SSE, so read the `data:` line:

```python
import json, os, urllib.request

BASE = "http://tana-mcp-ro.tana-mcp.svc.cluster.local:8765/mcp"
HDR = {"Authorization": f"Bearer {os.environ['TANA_RO_TOKEN']}",
       "Content-Type": "application/json",
       "Accept": "application/json, text/event-stream"}
sid = {}

def rpc(method, params=None, notify=False):
    msg = {"jsonrpc": "2.0", "method": method}
    if not notify:
        msg["id"] = 1
    if params:
        msg["params"] = params
    headers = dict(HDR, **({"Mcp-Session-Id": sid["v"]} if sid else {}))
    req = urllib.request.Request(BASE, method="POST", headers=headers, data=json.dumps(msg).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.headers.get("Mcp-Session-Id"):
            sid["v"] = r.headers["Mcp-Session-Id"]
        ctype, raw = r.headers.get("Content-Type", ""), r.read().decode()
    if notify:
        return None
    if "text/event-stream" in ctype:  # SSE: the JSON-RPC response is the data: line
        raw = next(line[5:] for line in raw.splitlines() if line.startswith("data:"))
    return json.loads(raw)

rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "haku", "version": "0"}})
rpc("notifications/initialized", notify=True)
print([t["name"] for t in rpc("tools/list")["result"]["tools"]])
```

Then add `tools/call` for `search_nodes` etc. The reach and bearer gate are
confirmed; verify the authenticated `tools/list` on your first run, and record the
working pod recipe in `memory/` so later runs reuse it.

## What to mine

Resume from a bookmark in `memory/` (e.g. "tana: through 2026-06-18") and look at
what's recent in the operator's graph:

- **Recent daily notes** — walk the last ~1–2 weeks of daily/calendar nodes (find
  them via `search_nodes`, then `get_children` to read them) for tasks the operator
  jotted but never actioned: "follow up on X", "ask Y", half-captured ideas, items
  with no owner or date.
- **Recently-touched nodes** — `search_nodes` for what the operator edited lately; a
  node they left open often implies intended next work, and is a window into what
  they're focused on right now.
- **Stale open tasks** — unchecked task nodes that have sat untouched, especially
  ones implying a deadline or an easy win.

Turn the worthwhile ones into items: `suggestion` for "do this", `prepared_prompt`
where a full-access agent could carry it out (embed the node title + date + the
desired outcome). Evidence in `body`: node title, date, a short quote — **never**
dump raw node bodies. Skip anything already done elsewhere and anything you've filed
before.
