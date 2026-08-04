# Personal Agents — Success Criteria

Pass conditions for the first stand-up experiment. Each is a **test with an
observable outcome**, not a judgment call, and each names the hazard that
`findings.md` says is most likely to make it fail. An experiment that passes S1–S4
justifies moving personal data onto the stack; one that doesn't tells us which
seam to fix first.

**Requirement strength is not uniform.** S1–S4 are hard requirements: the agent
must be able to open a PR and must be able to persist memory, and it must not
have unrestricted network. S5 is a **want, not a requirement** — the preference
is that the agent's _hands_ (its execution) sit in a separate, stronger sandbox
rather than the whole harness living in one; running the whole harness under a
single sandbox is an acceptable outcome, just a weaker one.

The criteria were deliberately about the _agent as deployed_, not about OpenClaw
in the abstract: the initial experiment ran against the former
`openclaw-gateway` plus OpenShell sandbox in this cluster.

## S1 — An agent is stood up and drives itself

**Test**: a fresh agent identity (not the existing personal one) is provisioned
declaratively — GitOps only, no `kubectl` mutation — reaches the gateway, is
reachable from the web UI, and completes a multi-turn task that requires at
least one `exec` round-trip into the sandbox.

**Pass**: the task completes without manual intervention, and re-running
`flux reconcile` reproduces the agent from scratch.

**Hazard**: the sandbox name-length shim (`openshell-cli-compat.yaml`) is a
stopgap for a blocked upstream PR; a new agent id long enough to trip it fails
here first.

## S2 — It opens a pull request against ducktape (hard requirement)

**Test**: instruct the agent to make a trivial change (a docs typo) on a branch
and open a PR against `agentydragon/ducktape`.

**Pass**: the PR exists, is authored by the agent's own identity, CI runs on it,
and the diff contains only the intended change — reached **across as many turns
as the work naturally takes**. Real software work is iterative: clone, look
around, edit, run something, fix, commit, push. A workflow that only survives if
the whole operation is crammed into one `exec` call is a **failure of this
criterion**, not a workaround for it. The checkout must still be there, intact,
on the next turn.

**The criterion is harness-agnostic; the hazards below are not.** S1–S5 are
phrased so any harness can be judged against them. Everything from here to the
end of this section is OpenClaw-with-OpenShell specific — how _that_ candidate
is expected to fail the criterion — and should be ignored when scoring a
different stack.

**Hazard (OpenClaw/OpenShell)**: the retention bug (findings.md, C7/C8). A
`git clone` that outruns `yieldMs` returns on yield, the post-`exec` sync
captures a partial tree, and the next `exec` re-uploads that partial state.
Separately, the mirrored workspace root is itself a git repo and root-level
`.git` is excluded from the sync back, so a clone placed _at_ the workspace root
loses its history in a way a clone in a subdirectory does not.

**Options if it fails, in increasing order of departure from today's setup**:
`mode: remote` (no per-turn sync, workspace stays put); OpenClaw's cloud-worker
path, whose git-based sync already solves this properly and makes PR authorship
first-class (findings.md, C8); or a different harness entirely.

## S3 — A permanent instruction survives into a later session (hard requirement)

**Test**: in session A, **explicitly invoke the persistence mechanism** —
"remember that …", "save this to your memory: …" — with a durable instruction
that is cheap to verify and has no side effects ("always use British spelling in
commit messages"). End the session. In session B — a **new session**, after a
gateway restart, and ideally after a sandbox recreation — give it a task where
the instruction applies, without restating the instruction.

Phrasing it as an explicit save is deliberate: what is under test is whether a
memory **persists and is recalled**, not whether the agent spontaneously decides
something is worth saving. If the explicit form is what reliably triggers the
harness's persistence path, use it. (Whether the agent volunteers to persist
unprompted is a separate, softer question — worth noting if observed, but it
does not gate this criterion.)

**Pass**: the instruction is honoured in session B, and the mechanism that
carried it is identifiable — which store holds it and which component wrote it.

**A path that _should_ work today, and needs no memory tools — UNVERIFIED, and
the point of running S3 is to check it.** Read from the source, not observed:
`MEMORY.md` is loaded as a workspace context file with its **content inlined into
the system prompt** of every session (findings.md, C7/C8), which would make the
loop: agent appends to `MEMORY.md` inside an `exec` → post-`exec` sync carries it
to the gateway workspace (it is not on the sync exclusion list) → the next
session's bootstrap inlines it, with no retrieval step and no `memory_*`.

**Every link in that chain is an assumption about workspace persistence**, and
each can fail independently: that the sandbox write reaches the gateway copy at
all; that it survives the next `exec`'s wipe-and-re-upload; that bootstrap
re-reads a `MEMORY.md` it did not itself author; and that sandbox recreation
re-seeds from the gateway rather than from a template. Do not treat any of it as
established until S3 has actually been run — and if it fails, the failing link
is the finding.

**Hazards, in order:**

1. **The retention bug** — a memory write that lands after a yield is lost like
   any other post-yield work. This is the one that can actually fail the test.
2. **The `session-memory` hook is a separate writer** into the same mirrored
   tree, and the post-`exec` replace can drop its writes unconditionally. That
   affects the automatic daily logs, not an instruction the agent deliberately
   wrote.
3. `memory_get`/`memory_search` are currently stripped by the sandbox tool
   policy, which costs **retrieval, not persistence** — worth fixing, but not a
   blocker for this criterion.

**Stretch**: the instruction survives a sandbox recreation and a gateway
restart, not just a new session. That distinguishes "remembered" from "still in
the mirror".

## S4 — It cannot reach arbitrary Internet

**The allowlist must be by domain, not by IP.** CIDR/`ipBlock` filtering does not
satisfy this criterion even though it technically blocks `example.com`: the
ranges belong to whoever owns them today, they drift, a shared CDN range grants
far more than intended, and "allow GitHub" ends up meaning "allow 52 prefixes
that also carry other tenants". A conforming setup enforces an **FQDN
allowlist** — an intercepting proxy the workload cannot bypass, Cilium
`toFQDNs`, or the sandbox runtime's own domain rules. Kubernetes
`NetworkPolicy` alone cannot express this, so it can only ever be the outer
fence that forces traffic into whatever does.

**Test**, from inside the sandbox via `exec`:

- `curl https://example.com` — must fail
- direct-IP egress (`curl https://1.1.1.1`) and raw DNS (`dig @8.8.8.8`) — must
  fail, so a pass isn't just DNS filtering
- the allowed path still works: GitHub reachable via the attached
  `agentydragon-github` provider, LLM traffic only via LiteLLM
- unsetting proxy env vars inside the command does not restore egress

**Pass**: all denials hold and the allowed paths work, with the deny coming from
the GitOps-owned policy (`cluster/k8s/agents/openshell/openclaw/policy.yaml`,
`network_policies: {}` default-deny) rather than from image defaults.

**Scope caveat that must be stated in the result**: this confines the
**sandbox**, where `exec` runs. It does **not** confine the gateway, which holds
the credentials, the memory files, and the tool implementations, and which is
only NetworkPolicy-confined. An S4 pass is therefore not "the agent cannot reach
the Internet" — it is "the agent's shell cannot". That gap is exactly what S5
closes.

### Waived for `public-coder-agent`, and only for it

Egress confinement is **switched off** for the public coding agent, by decision
rather than by failure — it passed S4 and the passing configuration is kept
commented out in the manifests, one uncomment away.

The reasoning, which is worth separating from "we gave up": that agent's job is
opening pull requests against arbitrary public repositories and reading whatever
they link to, so the allowlist is permanent friction. And what the allowlist was
protecting has moved. Once the GitHub PAT lives in the proxy and the substitution
is scoped by host (F15, F16), the credential boundary no longer depends on where
the agent can connect: the token is attached to GitHub and nowhere else however
far it reaches.

What is genuinely lost is a **data** boundary. With the world reachable, a prompt
injection from a cloned repository can send repository contents or session memory
out. That is an accepted trade for an agent whose inputs are public and whose
output is a pull request.

**S4 remains a hard requirement for every other agent here** — the personal-data
agent especially, where the material is the thing being protected and no
credential-scoping trick substitutes for not being able to reach an exfiltration
endpoint. This waiver does not generalise, and the tested configuration exists
precisely so it does not have to be rebuilt.

## S5 — The whole harness runs sandboxed (weaker containment, accepted tier)

**Test**: OpenClaw itself runs under OpenShell and/or is provisioned through a
kagent `AgentHarness` CRD with declarative `allowedDomains`, rather than as an
ordinary pod that happens to delegate `exec`.

**Pass**: the gateway process is confined, its egress is policy-driven, and the
S4 probes fail when run from the gateway itself, not only from the sandbox.

**Status**: explicitly the fallback tier. It is weaker than a per-agent
network-isolated exec model, and findings.md notes `AgentHarness` already targets
`openclaw`/`openshell`/`nemoclaw` with declarative `allowedDomains` — so it is
cheap to try and worth knowing about, but settling here should be a deliberate
sigh, not a default.

## Not success conditions (yet)

Recorded so they don't quietly become scope:

- **W1 credential substitution** — a want, not a requirement; OpenShell already
  does it, so it is a bonus observation if it shows up, not a gate.
- **K1–K5 knowledge garden** — separate track; needs a durable-memory answer
  (S3) before the garden question is even well-posed.
- **Full trace/transcript export** — C6 is a real gap but does not gate standing
  an agent up.
