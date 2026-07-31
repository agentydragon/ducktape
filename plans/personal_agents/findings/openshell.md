# OpenShell

Findings are numbered in discovery order across the whole programme and cited by
number from cluster manifests, so the IDs are stable and non-contiguous here.
Index of all findings: [README.md](README.md).

## F1. A second supervisor invocation breaks the sandbox's SSH relay, permanently

**What is established, by a two-way controlled test.** The sandbox does not
wedge with age, with load, or on its own. `kubectl exec` against the sandbox pod
breaks it immediately and permanently, until the sandbox is deleted and
recreated.

**What is not established: that this was the production trigger.** The operator
reports that the 2026-07-28 production wedge happened with nobody exec'ing into
the pod — just a conversation with the agent, during which the agent itself
observed the breakage. So `kubectl exec` is _a_ sufficient trigger, not _the_
cause, and at least one other path into the same broken state exists. See the
open question at the end of this finding.

The cycle, run end to end on `oc-abcf6689` (2026-07-29 16:1x):

| Step                                            | OpenClaw `exec` result                                    |
| ----------------------------------------------- | --------------------------------------------------------- |
| Sandbox recreated, driven only through OpenClaw | **works** — returned `ZQ1_MARKER`, `hostname`, `uid=1000` |
| One `kubectl exec … -c agent -- true`           | —                                                         |
| Same OpenClaw `exec`, immediately after         | **fails** — `kex_exchange_identification`                 |
| Delete sandbox, let the gateway recreate it     | **works** again — returned `ZQ3_RECOVERED`                |

A single no-op `kubectl exec -- true` is enough. Nothing else changed between
the working and broken runs.

**Mechanism.** The sandbox pod's container command _is_ the supervisor,
`/opt/openshell/bin/openshell-sandbox`, and the SSH endpoint it serves is a Unix
socket, not a port:

```text
OPENSHELL_SSH_SOCKET_PATH=/run/openshell/ssh.sock
```

`kubectl exec` re-invokes that same binary as
`openshell-sandbox --mode=process -- <your command>` (visible in the sandbox's
own process table). That process-mode invocation re-binds the same socket path,
unlinking the long-lived listener's socket and leaving it orphaned. Sampling
`/proc/net/unix` across probes shows exactly that shape — one stable inode from
PID 1 plus a second inode that changes on every `kubectl exec`, with
`/run/openshell`'s mtime tracking each one:

```text
15:58:02  inode=561010904   inode=561142552
15:58:24  inode=561010904   inode=561154142
15:58:46  inode=561010904   inode=561152575
```

Afterwards the path resolves to a socket whose owning process has exited, so the
gateway's relay gets `ECONNREFUSED` and the SSH handshake dies before key
exchange. The three layers report it differently, which is why it took so long
to line up:

| Layer      | What it says                                                                     |
| ---------- | -------------------------------------------------------------------------------- |
| Gateway    | `ForwardTcp: relay target open failed … error=Connection refused (os error 111)` |
| Supervisor | `supervisor session: relay bridge failed`                                        |
| OpenClaw   | `kex_exchange_identification: Connection closed by remote host`                  |

The gateway's `CreateSshSession` still returns `200` throughout — the session is
created fine; only the relay's connect to the sandbox fails. The re-bind step is
the most probable mechanism rather than a directly observed one:
`/run/openshell` is `drwx------ root root` and `kubectl exec` lands as uid 1000,
so the socket cannot be connected to or inspected by hand from inside.

**Corrections to earlier versions of this note.** Both of the following were
published here and are wrong:

- _"Sandboxes wedge with age."_ They do not. A 2-minute-old sandbox and a
  19-hour-old one fail identically, and both were fine until exec'd. The apparent
  correlation with age was really a correlation with _how long I had been
  poking at them_.
- _"The fault sits above TCP, in the gateway↔supervisor session layer, and
  nothing observable distinguishes a wedged sandbox from a healthy one."_ It is
  a Unix socket that gets re-bound, and the distinguishing evidence
  (`/proc/net/unix`) was observable the whole time — I was looking for a TCP
  listener with `ss -lnt`, which by construction could never show it.

The nine hypotheses previously killed by direct test (gateway restart, GitHub
provider, settings CronJob, OIDC expiry, OpenShell gateway restart,
idle-shutdown policy, mTLS rotation, leaked processes, dead TCP relay) were all
genuinely killed — they were just all pointed at the wrong layer.

**Two method traps that produced wrong intermediate conclusions**, recorded so
they are not repeated:

- Replaying the plugin's SSH by hand (`openshell sandbox ssh-config` →
  `ssh -F`) fails even against a _healthy_ sandbox, because the plugin applies
  `applyGatewayEndpointToSshConfig` before use. An "everything is wedged"
  reading from that method was an artifact.
- The first attempt at the virgin-sandbox test deleted the wrong sandbox. An
  agent's `agentId` — not its `OpenClawInstance` — selects the sandbox, so
  `oc-lab` was bound to `oc-abcf6689` while I was deleting and inspecting
  `oc-9818946b`. Always resolve the target through the `sandbox_id` in the
  gateway's `relay open failed` line, not through which instance you created.

**Operational consequences.**

1. **Never `kubectl exec` into a pod in `openshell-sandboxes`.** It is a
   destructive operation on a live agent, not a read-only diagnostic. This is
   the single most surprising thing found in the whole lab, and nothing warns
   you: the exec succeeds, prints normally, and exits 0.
2. **Recreating the sandbox is the complete remedy** and costs only whatever is
   outside the retained workspace PVC.
3. **`oc-plain` cannot hit this at all**, since it has no relay and no sandbox.

**Open question — what triggered production.** The production sandbox was
created 20:34:01 and first failed 20:40:55, with no `kubectl exec` in between.
Whatever re-binds the socket therefore has at least one other caller. Tested and
ruled out so far:

| Candidate trigger                          | Result                                                         |
| ------------------------------------------ | -------------------------------------------------------------- |
| OpenClaw's `process` tool                  | Not a trigger — healthy afterwards, 0 relay failures           |
| Ordinary `exec` tool use                   | Not a trigger — dozens of round-trips on a healthy sandbox     |
| Six abandoned yielding background sessions | Not a trigger — healthy during, and after they exited          |
| A `git --version` / `gh --version` probe   | Not a trigger — including `gh`, which is absent from the image |

The last two were the leading hypotheses, both drawn from what that session had
actually just done, and both are now dead. Still untested: a second client
attaching to the same sandbox (two
`OpenClawInstance`s sharing an agent id already collide this way, see Rough
edges); a supervisor reconnect after a gateway restart; and anything in the
operator's reconcile loop that touches the pod. Until one of those reproduces
it, the honest statement is that the _state_ is understood and one route into it
is proven, while production's route is not.

## F2. Sandbox egress policy is per-process, not per-pod

`kubectl exec` into a sandbox pod gets **unrestricted** egress: `example.com`
200, direct-IP 301, raw DNS to 8.8.8.8 resolving, and no proxy variables in the
environment. The agent's own commands in the _same pod_ are filtered — its probe
returns `CONNECT tunnel failed, 403`.

So OpenShell confinement is a property of **how the process is launched** (the
supervisor's netns/proxy), not of the pod. Anyone who can open a shell in the
sandbox by another route is outside the policy, and there is no Kubernetes
NetworkPolicy restricting egress from `openshell-sandboxes` — the only policy
there is _ingress_ for SSH on 2222.

## F3. Operator-managed OpenClaw cannot be egress-confined (blocks S4)

The OpenClaw operator's generated NetworkPolicy always contains an egress rule
with **no destination selector on port 443/TCP** — i.e. all HTTPS, anywhere. The
CRD's `allowedEgressCIDRs` field ("Default allows all egress on port 443 for AI
APIs") **appends** rather than replaces: after setting it, the wide-open rule was
still present in the rendered policy.

Verified from inside the harness container:

```text
direct, proxy env stripped, example.com: 200
via proxy example.com:                   blocked
via proxy api.github.com:                200
```

**Confinement in the operator shape is therefore advisory.** It depends on the
workload honouring `HTTP_PROXY`/`HTTPS_PROXY`; a process that ignores them — or
one `env -u` — has unrestricted HTTPS. **This applies to production
`openclaw-gateway` as well**, whose egress control is the same Kyverno-injected
proxy variables plus this same policy.

Consequence for the plan: a conforming S4 setup needs a shape where the
NetworkPolicy is ours. Since the operator is not a requirement, the next
configuration is OpenClaw as a plain Deployment.

## F11. Running the whole harness under OpenShell is not possible on the k8s operator — three measured blockers

NemoClaw does exactly what we want, on the Docker driver. The question was whether
the Kubernetes operator can express the same thing. Tested rather than reasoned:
**no**, and for three independent reasons.

**How NemoClaw actually does it** (read from `NVIDIA/NemoClaw` source):

- The sandbox image ships a launcher and the container is created with it as the
  startup command — `openshell sandbox create … -- nemoclaw-start`, persisted by
  the Docker driver as `OPENSHELL_SANDBOX_COMMAND`. Legacy sandboxes instead used
  `OPENSHELL_SANDBOX_COMMAND=sleep infinity` with the launcher as a sibling.
- `nemoclaw-start` runs as root and launches `openclaw gateway run --port …` **as a
  separate `gateway` user**, then steps agent commands down to `sandbox` with
  `setpriv`. The stated reason: "the gateway runs as a separate user so the
  sandboxed agent cannot kill it or restart it with a tampered config (fake-HOME
  bypass)". A watchdog pattern-matches the cmdline before killing anything.
- `agents/openclaw/manifest.yaml` declares the contract:
  `gateway_command: "openclaw gateway run"`, `health_probe` on 18789,
  `forward_ports: [18789]`, `inference.provider_type: gateway_managed`.
- The community image carries the same idea in miniature: `/usr/local/bin/openclaw-start`
  documents `openshell sandbox create --from openclaw --forward 18789 -- openclaw-start`.

**Blocker 1 — the command knob is refused by name.** Setting it through the CRD's
`spec.environment` (a plain `map[string]string`) fails at the gateway:

```text
gateway rpc error: Client specified an invalid argument
  "spec.environment keys starting with OPENSHELL_ are reserved;
   got 'OPENSHELL_SANDBOX_COMMAND'"
```

**Blocker 2 — the image entrypoint is overridden.** Created a sandbox with
`spec.image` set to the community OpenClaw image and no override. It reached
`Ready`, and the pod runs the supervisor, not the image's launcher:

```text
cmd=["/opt/openshell/bin/openshell-sandbox"]
image=ghcr.io/nvidia/openshell-community/sandboxes/openclaw:latest
logs: only openshell_supervisor_* lines — no "OpenClaw gateway starting in background"
```

So `openclaw-start` never runs. The manifest-declared command is a CLI-driver
concept; the k8s driver discards it.

**Blocker 3 — a sandbox cannot be reached.** No container ports, no Service in
`openshell-sandboxes`, and the only ingress is from the gateway pod on TCP/2222.
Our gateway has to answer the Authentik outpost on 18789. NemoClaw sidesteps this
by being a CLI you `connect` to and forwarding ports client-side.

**Upstream confirms it is unbuilt, not undiscovered.** `openshell-k8s-operator`
at HEAD, `crates/operator/src/crd.rs:25`:

```rust
/// selection, entrypoint, and TTL/cleanup arrive in later milestones.
```

There is no command or port support anywhere in the operator crates.

**Verdict.** The capability exists in OpenShell and is used in production by
NVIDIA's own product; it is the **Kubernetes operator** that does not surface it.
Blockers 1 and 2 are one upstream milestone away. Blocker 3 is the deeper one — a
sandbox is modelled as something you exec into, not something that serves — and
nothing suggests it is coming. Revisit when the operator gains both an entrypoint
and a way to address a sandbox as a network service.

## F13. OpenClaw _does_ run inside an OpenShell sandbox on the Docker driver — built and run from scratch

F11 established the Kubernetes operator cannot do this. This is the same stack on
the Docker driver, stood up from nothing in the agent container, to see how far it
gets. **Far enough to watch OpenClaw's gateway start inside a sandbox.**

What was needed, in order — none of it documented in one place:

1. **A gateway with the Docker driver.** `OPENSHELL_DRIVERS=docker`, or
   `compute_drivers = ["docker"]` in a TOML config. Auto-detection goes
   Kubernetes → Podman → Docker.
2. **Gateway JWT auth.** Docker sandboxes are refused outright without it:
   `docker sandboxes require gateway JWT auth; configure
[openshell.gateway.gateway_jwt]`. The repo's `e2e/configs/gateway/docker.toml`
   is a working template.
3. **A supervisor binary.** The driver bind-mounts a host-side
   `openshell-sandbox` into each sandbox. `supervisor_image` alone was not enough;
   extracting `/openshell-sandbox` from `ghcr.io/nvidia/openshell/supervisor:0.0.90`
   to a real path and setting `supervisor_bin` was.
4. **Path identity between gateway and daemon.** This was the recurring trap. The
   gateway hands _host_ paths to Docker, so anything it writes inside its own
   container is invisible to the daemon. Symptoms are misleading: a missing
   supervisor appears as `exec: "/opt/openshell/bin/openshell-sandbox": is a
directory`, and a missing JWT as `failed to read sandbox token from
/etc/openshell/auth/sandbox.jwt`. Fix: mount one directory at the _same path_
   inside and out, and point `HOME`/`XDG_STATE_HOME`/`supervisor_bin` at it.
5. **A reachable callback.** Sandboxes call `OPENSHELL_ENDPOINT`, defaulting to
   `host.openshell.internal:<port>`, which does not resolve. Running the gateway
   with `--network host` and setting `host_gateway_ip` fixed it.

Then, with no `openshell` CLI at all — it cannot be fetched here — the gateway was
driven directly over gRPC using the protos from the repo and `grpcurl`
(reflection is disabled, so `-import-path`/`-proto` is required):

```text
CreateSandbox(image=ghcr.io/nvidia/openshell-community/sandboxes/openclaw:latest)
  -> SANDBOX_PHASE_READY
ExecSandbox(command=["/usr/local/bin/openclaw-start"])
  -> "OpenClaw gateway starting in background."
     "  UI:   http://127.0.0.1:18789/"
```

**The startup-command mechanism is confirmed live.** The driver sets it on the
container itself:

```text
OPENSHELL_SANDBOX_COMMAND=sleep infinity
OPENSHELL_ENDPOINT=http://host.openshell.internal:8080/
OPENSHELL_SANDBOX=oc-docker
```

That is the exact variable NemoClaw overrides with `nemoclaw-start`, and its
default here is the `sleep infinity` keepalive its source calls legacy.

**Why exec is not a substitute for it.** The gateway _did_ start under
`ExecSandbox`, and then died with the exec session:

```text
curl http://127.0.0.1:18789/  -> HTTP=000
openclaw processes            -> 0
```

`openclaw-start` backgrounds with `nohup … &`, and it still did not survive. So a
long-lived harness genuinely requires being the sandbox's **startup command**, not
an exec child — which is exactly why NemoClaw sets the variable rather than
exec'ing, and why F11's blockers are load-bearing rather than cosmetic.

**Recreating the container by hand to set that variable did not work**, and the
failure is informative rather than embarrassing: the rebuilt container lost
network wiring and could no longer reach the gateway. NemoClaw devotes a tested
module (`recreateOpenShellDockerSandboxWithStartupCommand`) to this step, which is
a fair signal of how much state a sandbox container carries.

**Bottom line.** The Docker driver runs OpenClaw under OpenShell, and a
Docker-in-Kubernetes deployment of it is a real option. The cost is everything
above — a second gateway, a Docker daemon, shared host paths, host networking for
callbacks, and out-of-band container surgery to set a startup command — for a
_want_ (S5) rather than a hard requirement. See TODO; not recommended today.
