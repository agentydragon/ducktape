# Minimize public-coder approval stalls without widening authority

Status: proposal (2026-08-21)

## Objective

`public-coder-agent` should ask the Operator to approve only work that genuinely needs the
Operator's authority. Its ordinary public-repository development, local validation, and existing
read-only Kubernetes diagnostics should continue without waiting on Haku Console.

There are two complementary ways to remove those stalls:

1. route calls to an already-authorized local or direct identity when one exists; and
2. reshape frequently needed approval-gated operations into typed tools or conditional policies
   that are narrow enough for the Operator to auto-approve deliberately.

The second category is not limited to deterministic predicates. Haku Console may use a
programmatic check, an LLM-evaluated predicate, a hybrid of the two, or blanket approval for a tool
whose implementation is safe by construction. The design question is not "can an LLM ever approve
anything?" It is "what bounded affordance and policy would the Operator be comfortable delegating?"

The durable agent-facing playbook derived from this audit landed in
[PR #4517](https://github.com/agentydragon/ducktape/pull/4517). This plan now contains only the audit
evidence and proposed future Haku affordances.

## Audit evidence

A scan of the OpenClaw session JSONL available in the public coder container found 1,335 assistant
invocations of generated `haku-console__*` tools across the retained sessions. That number includes
226 follow-up `get_tool_call` reads and other console bookkeeping; it is not a count of unique
privileged effects. The largest operation groups were:

| Surface                | Calls seen | Important examples                                                         |
| ---------------------- | ---------: | -------------------------------------------------------------------------- |
| Kubernetes passthrough |        598 | 327 `resources_get`, 85 `pods_exec`, 65 `resources_list`, 32 `pods_log`    |
| Host execution         |        193 | almost entirely `wyrm2` as `agentydragon`                                  |
| GitHub                 |        240 | 40 PR reads, 35 file reads, 34 PR creates, 23 issue reads, 10 issue writes |
| Console bookkeeping    |        275 | `get_tool_call`, `list_tool_calls`, reflection/status, withdrawal          |
| Other                  |         29 | Sandbox, Recall, Grocy, and routine calls                                  |

The Kubernetes calls were concentrated in `flux-system` (154), `public-coder-agent` (97),
`ducktape-flux` (69), `haku-openclaw-spike` (60), `seaweedfs` (46),
`vm-images-publisher` (44), and `grocy-sf` (38). Thirteen generic reads targeted Secrets; nine
calls created or updated resources; 85 calls executed in Pods. Those latter classes are not safe
candidates for unconditional approval.

The retained OpenClaw log is not the authoritative Haku ledger and may omit expired sessions. The
ledger remains the source for the final status, approving Operator, policy id, and evaluation of an
individual call. Untruncated ledger snapshots available during this audit yielded only a 63-call
lower bound; other snapshots were response-truncated and repeated calls. Observed auto-approved
examples included repository-scoped PR search, job-log, and PR-read calls. Manual examples included
Recall search, routine launch, GitHub writes, cluster-admin Kubernetes, and host execution. The local
log is still sufficient to show the routing pattern that caused stalls.

## Landed operational baseline

PR #4517 landed the day-to-day routing and approval lifecycle in
`cluster/k8s/agents/public-coder-agent/TOOLING.md`. This plan does not repeat those instructions.
Its remaining scope is the audit evidence and future Haku tool, schema, metadata, and policy changes.

The implementation proposals below assume the landed boundaries remain in force: local Pod state
stays local, direct identities retain their own authorization boundaries, and Haku supplies only
authority or services unavailable through those direct surfaces.

## Recommended affordances and predicates

### 1. Prefer no new Haku rule for ordinary GitHub work

The direct iron-proxy path already has the correct identity and audit trail on GitHub. Adding
Operator-OAuth auto-approval for PR writes would solve latency by using the wrong principal. Keep
Haku's repository-scoped reads for the private repository, but document direct GitHub as the public
coder's primary write surface.

If structured MCP ergonomics are still desired, add a separate `github-public-coder` server whose
backend credential is `agentydragon-agent`, not the Operator connection. A deterministic policy may
auto-allow only:

- branch/file pushes in `agentydragon-agent/ducktape`;
- draft PR creation with head owner `agentydragon-agent` and base
  `agentydragon/ducktape:devel`; and
- title/body/state edits or comments on PRs authored by `agentydragon-agent`.

Do not include merge, workflow dispatch/rerun, repository settings, collaborator management,
release publication, or writes to upstream branches.

### 2. Integrate with the existing Kubernetes-grant work rather than inventing a second path

The Kubernetes direction is already tracked separately: Haku-backed kubectl should provide the
public-coder ServiceAccount scope by default and allow explicitly requested temporary grants. This
proposal should not create a competing reader or grant design.

One approval policy from this audit does fit that work: before accepting a generic `hostexec/bash`
request containing kubectl, evaluate whether the complete command could be executed with the
public-coder RBAC supplied to the policy. If yes, reject the Haku request with an actionable message
to use the caller's own kubectl credentials. This is a routing decision, not a cluster authorization
decision; the direct API server and ServiceAccount still enforce the actual request.

### 3. Separate intended host escape-hatch use from replaceable patterns

Hostexec is intentionally the high-permission escape hatch to physical operator machines. A call
can run as `agentydragon` on `wyrm2` or `rugged`, using host-local state and permissions unavailable
inside the public-coder Pod. Central valid examples are admin-level kubectl commands using the
operator's admin credential, reading a machine-specific failure from `/var/log/...`, inspecting
host services/devices/networking, and performing an explicitly requested host-side administrative
operation.

The goal is not to route those calls away from hostexec. It is to distinguish them from calls that
used the escape hatch only to reproduce public source, GitHub, or standing-RBAC information already
available in the Pod.

The 193 retained hostexec calls were not mostly service-status or journal queries. A command-level
classification found:

| Hostexec shape                  | Calls | Typical operations                                                                  |
| ------------------------------- | ----: | ----------------------------------------------------------------------------------- |
| kubectl/cluster                 |   121 | Node inventory/describe/events; Flux status; workloads, Pods, logs, PVCs, sandboxes |
| repository/source inspection    |    41 | `git log/status/fetch/worktree`, `rg`/`grep`, public PR/API inspection              |
| mixed host-only administration  |    22 | private checkout inspection, SOPS work, credential/tool presence, package probing   |
| direct HTTP/log API diagnostics |     6 | Loki queries, GitHub Actions logs, Authentik inventory checks                       |
| build/tool bootstrap            |     2 | locate SOPS; temporary AWS CLI                                                      |
| host-system inventory           |     1 | locate installed OpenClaw/Node/container tooling                                    |

These counts are heuristic and include retries, but they identify concrete affordances. A later
focused recheck found 198 retained wrapper records after additional calls and probes were made; the
recommendations depend on the repeated command shapes rather than an exact lifetime total.

#### Do not add typed host checkout, environment, or log readers

Do not add `host_checkout_read`, `host_environment_probe`, or `host_log_read`.

Host checkout and environment questions are meaningful precisely because they concern a particular
physical machine. The existing manually reviewed Bash surface can answer those questions, and a
separate typed reader would duplicate the same host authority while obscuring why the host matters.
Stable operating facts such as the current `bash -c` semantics, target-account environment, named
NixOS hosts, and Ducktape direnv invocation belong in the public-coder tooling guide after they are
verified.

The historical calls also do not demonstrate a recurrent safe host-log shape that justifies a
special reader. A request to inspect a specific host failure under `/var/log/...` remains a valid,
narrowly bounded manual hostexec use. Reconsider a typed log reader only if later evidence shows a
repeated service/path/redaction contract rather than speculating one in advance.

#### Reject operations that already belong elsewhere

- Repository/source inspection of public Ducktape should be rejected by an LLM routing predicate
  when the requested fact is reproducible from the checkout in the public-coder Pod or the public
  GitHub remote. A question about a physical host's dirty worktree, worktree layout, local-only ref,
  remotes, or direnv state is genuinely host-specific and should remain a narrowly bounded manual
  hostexec request.
- Hostexec kubectl that is within the supplied public-coder RBAC should be rejected with a message
  to use direct kubectl. Calls requiring broader scope remain eligible for the normal Haku/grant
  path.
- Public GitHub PR state, check runs, logs, branches, and source reads should use the direct
  `agentydragon-agent` credential or an existing scoped read tool.

#### Extend the existing Loki read path

Do not add a second standalone `loki_log_query` service. Ducktape already deploys
`cluster/proxies/loki_read_proxy`, which exposes only read-only instant/range log queries, requires
an exact allowlisted namespace matcher, caps result size, and reaches Loki through restricted
network policy. Its current application has no authentication of its own; ingress NetworkPolicy
limits access to Haku sandbox Pods.

A repository and live-catalog recheck found no dedicated Loki MCP server or tool currently reflected
to `public-coder-agent`. The next design should therefore put an authenticated Haku/MCP surface in
front of the existing proxy, or extend an existing Haku sandbox integration to call it, rather than
creating another Loki query implementation.

Replace or supplement the proxy's static namespace allowlist with caller-aware authorization. A
query should be permitted only where the caller's effective Kubernetes/Haku authorization allows
the corresponding Pod logs, using the existing Kubernetes authorization/grant direction rather
than a new parallel policy system. Preserve the proxy's exact namespace pinning, read-only methods,
query and time-range restrictions, result caps, and restricted Loki egress.

The rarer operations should stay manual or use existing purpose-built surfaces: SOPS re-encryption,
credential inspection, arbitrary package unpacking, Authentik administration, and secret-backed S3
canaries do not justify broad host auto-approval.

### 4. Allow configured LLM predicates to grant conditional auto-approval

An LLM-evaluated predicate is a legitimate Haku Console policy mechanism when the Operator has
chosen the bounded rule. The LLM is not merely advisory: its `approve`, `manual`, or `reject`
decision may control whether the call executes automatically.

Recommended policy contract:

- pass the full normalized tool arguments, caller identity/profile, tool description/schema, and
  the exact static context needed by the rule;
- give the predicate a short, single-purpose policy rather than a generic "is this safe?" prompt;
- fail closed to manual review on malformed input, uncertainty, model failure, or context overflow;
- record the model, policy version, decision, and concise explanation in the ledger; and
- use deterministic preconditions where cheap, leaving semantic classification to the model.

Concrete predicates follow. The Operator endorsed further design work on the two hostexec routing
predicates below; the other entries remain unevaluated candidates, not approved directions.

1. **`public_github_code_search`.** Deterministically require the read-only `search_code` tool and
   valid repository qualifiers. Then permit an LLM to approve searches scoped only to well-known
   public projects, rejecting private, personal, suspicious, missing, or ambiguous scopes. A GitHub
   metadata lookup can additionally verify public visibility before the model runs.
2. **`hostexec_direct_kubectl_router` (endorsed direction).** Give the model the command and current
   public-coder RBAC. If every requested Kubernetes operation is read-only and permitted directly,
   reject the Haku call and tell the agent to use direct kubectl. If the command also performs
   unrelated host work, touches Secrets/exec/writes, or is ambiguous, leave it manual.
3. **`hostexec_duplicate_source_route` (endorsed direction).** If a command requests source content
   or public remote state reproducible from the public-coder Pod or GitHub, reject it with the
   concrete Pod/direct route. Do not reject questions whose subject is the physical host's own
   checkout or environment. This distinction is contextual semantic classification, not a pathname
   comparison or static metric.
4. **`routine_log_diagnostic`.** After structural caps on time range, output, and allowed log
   backends, let a model approve ordinary application-error investigation while escalating broad,
   identity, authentication, or secret-adjacent queries.

LLM predicates should not be used to auto-approve unrestricted Operator-identity GitHub writes.
The direct `agentydragon-agent` path already provides the correct principal for ordinary public
contribution work.

### 5. Improve tool descriptions and metadata so callers can choose correctly

This audit exposed routing-relevant facts that are present in implementation comments or policy
code but not clearly advertised in the reflected tool catalog.

#### Describe the execution target and purpose, not only the operation

The current hostexec tool description says that it runs Bash on an operator machine and is approval
gated. Its server instructions explain more, but the generated tool description should itself say:

- the target is one of the specific named machines configured for hostexec, and is normally not the
  machine or container where the connecting Agent is running;
- the command runs as the requested POSIX user on that named machine;
- this is primarily an escape hatch for debugging that specific host or using host-local tokens,
  credentials, files, devices, services, or permissions unavailable to the caller; and
- the caller's own local execution, GitHub, or kubectl surfaces are preferable when they can answer
  the same question.

Avoid describing a generic "Pod tool": Haku Console does not provide one. Local execution is a
capability of a particular connecting harness such as OpenClaw, not part of the Haku tool catalog.

The arbitrary Bash tool currently has no MCP annotations. It should advertise
`readOnlyHint=false`, `destructiveHint=true`, `idempotentHint=false`, and `openWorldHint=true` so a
client does not mistake the absence of metadata for a routine read.

The cluster-admin kubectl passthrough and Operator-OAuth GitHub server need equivalent authority
summaries. The descriptions should make clear that they execute under the approving Operator's
principal, not `public-coder-agent` or `agentydragon-agent`.

#### Distinguish payload shape from auto-approval capability

`ToolMetadata.approval_mode` currently exposes only `passthrough` or `approval_required`. That names
the input schema shape, not the effective policy. A conditionally auto-approved GitHub read and a
fully manual hostexec call both appear as `approval_required`, even though
`ToolAutoApprovalMode` already distinguishes always, conditional, and manual modes internally.

Expose separate metadata, for example:

- `submission_mode`: `passthrough` or `approval_envelope`;
- `auto_approval_mode`: `always`, `conditional`, or `manual`; and
- optionally a non-secret policy label or explanation of the condition.

This lets an agent answer "what is auto-allowed?" from the caller-specific catalog without parsing
deployment YAML or learning by submitting a call. It also avoids implying that every envelope waits
for a human: a conditional predicate may approve it immediately after inspecting arguments.

#### Correct the generated approval preamble

The proxy instructions currently say that tools with the upstream schema auto-approve and describe
every enveloped call as waiting in the Operator queue. Revise that wording to explain all three
modes: transparent always-auto-approved tools, enveloped conditionally auto-approved tools, and
enveloped manual tools. The envelope is an Agent approval-lifecycle shape, not proof that a human
must act.

## Acceptance criteria for any implementation

- Public coder still cannot approve its own requests.
- No Operator OAuth or cluster-admin credential enters the agent container.
- New tools are denied by default and validated against owned schemas.
- Direct GitHub writes are attributable to `agentydragon-agent`.
- Kubernetes standing and temporarily granted authority remain enforced by the Haku kubectl/grant
  design and Kubernetes RBAC; routing predicates do not manufacture cluster permission.
- Unrestricted Secret, exec, host shell, merge, workflow mutation, and live-cluster write paths
  remain manual unless replaced by a separately reviewed narrower tool/policy.
- LLM predicates are explicit, versioned, fail closed, receive complete normalized arguments, and
  leave an auditable explanation when they approve, reject, or defer a call.
- Reflected metadata distinguishes schema/submission shape from effective auto-approval mode.
- The ledger distinguishes auto-approved policy execution from direct external operations; neither
  path is represented as Operator approval when no Operator acted.
