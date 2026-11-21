# Cluster VPA rollout

## Destination

VPA sizes **CPU and memory requests** for nearly all compatible workloads. Every
active container has intentional scheduling requests, including workloads that
cannot use VPA. The [VPA dashboard](https://agentydragon.github.io/ducktape/vpa/)
is the dated inventory and rollout evidence; this plan owns the target and the
remaining work, not another copy of the inventory.

The 2026-11-21 04:40 UTC snapshot covers 195 of 233 active Deployments,
StatefulSets and DaemonSets with a direct VPA. Among 574 regular containers in
Running or Pending Pods, 132 lack CPU requests and 132 lack memory requests
after admission. These are separate counts, not proof that the same containers
lack both. The 229 VPA objects also include Jobs and other targets; 66 were
`Recreate`, 99 `Initial`, and 64 `Off`. Refresh the dashboard for current state.

| Workload                                    | Target update mode | Resource ownership                                                                                           |
| ------------------------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------ |
| Replicated and restart-safe                 | `Recreate`         | CPU and memory requests where compatible; reviewed bounds                                                    |
| Singleton or restart-sensitive              | `Initial`          | CPU and memory requests where compatible; no VPA eviction                                                    |
| Insufficient history or uncertain ownership | `Off`              | Recommendations until a safe policy is established                                                           |
| Operator-managed, dynamic or short-lived    | Case by case       | VPA only where its owner, history and admission behavior are reliable; otherwise intentional static requests |

`Initial` applies at Pod creation; it does not resize a running Pod. `Recreate`
can evict a running Pod. Goldilocks' `auto` label currently produces `Recreate`.
Ntfy must not be evicted by VPA; keep it `Initial` until its availability design
and the chosen update mode make that safe. Electric, CDviz and other singletons
need the same workload-specific check, not automatic promotion.

For new policies, default to `RequestsOnly`; limits remain explicitly owned by
the workload. **Preserve existing `RequestsAndLimits` policies**, including the
implicit VPA default, unless a workload-specific change is reviewed. VPA scales
limits proportionally to requests in that mode. The LiteLLM cold-start outage
in [the VPA audit](../k8s/TODO.md#vpa-memory-audit--are-these-limits-really-needed)
shows why limit ownership matters. Do not infer safety from an absent policy:
the upstream VPA default controls both requests and limits.

CPU and memory are independent decisions. Keep an HPA's utilization resource
out of VPA control: a CPU-utilization HPA needs a stable CPU request, while VPA
can manage memory. Apply the symmetric rule to a memory-utilization HPA. Review
custom/external HPA metrics separately. A recommendation is not proof that a
startup burst, rare task, or peak is covered; retain explicit template floors
for first admission, unavailable recommendations, and owner-cache misses.

## Execute broad waves

Each wave covers a **coherent fleet cohort**, not one Deployment. Group
workloads with the same policy and evidence; include HA, PDB, placement,
authoring, and VPA changes together when they make that cohort safe. Use the
largest batch whose behavior and rollout can be reviewed and recovered. Split
only at a genuine dependency, materially different failure mode, or review
boundary; independent ready work proceeds in parallel.

Prepare the next wave while CI or rollout observation is in progress. Open
condition-blocked work as draft, with the missing condition in its title and
body; check it and mark ready when satisfied. Use [native GitHub stacks](../../devinfra/skills/github_stacks/SKILL.md)
for real content dependencies, not merely because changes may conflict. Verify
a wave as a group; do not impose a serial PR-and-observation cycle per workload.

### 1. Expand request sizing

- Inventory existing policies before changing them: mode, controlled resources,
  request/limit ownership, bounds, HPA metric, template and admitted Pod
  resources, startup and historical usage. Keep existing limit control and
  non-VPA-owned sidecars intact.
- Broaden existing memory-only policies to CPU **and** memory requests where CPU
  is not HPA-controlled and the observed range supports useful bounds. Apply
  `Initial` to compatible uncovered services in reviewed namespaces, including
  request gaps; leave automatic eviction as a separate choice.
- A first broad candidate is the nine replicated Agentplane staging services
  already in `Initial`: `actions`, `app`, `environments`, `history-service`,
  `llm-ingress`, `mcp-edge`, `notifications`, `policy-controller`, and
  `sessions`. Review CPU observations, startup needs and per-node headroom as a
  cohort; preserve their current memory policies and no-eviction mode. Electric,
  frontend, dynamic Sandboxes and other untargeted staging workloads have
  different ownership or availability requirements and are not implicitly in
  that cohort.
- In parallel, classify the larger `monitoring` and `kube-system` Initial fleets,
  MCP/application singletons, and Goldilocks-disabled namespaces. The 2026-11-21
  snapshot has 31 monitoring and 12 kube-system Initial active controllers;
  many monitoring workloads are singletons. These counts identify opportunity,
  not approval to flip a namespace-wide mode. Refresh evidence before choosing
  each batch.

### 2. Make more workloads safe for automatic updates

- Promote well-spread, replicated, restart-safe workloads to `Recreate` in broad
  cohorts after the checks below. Include controllers and webhooks: add replicas,
  node spread, rollout settings and PDBs when they improve availability, even
  when VPA is not the immediate reason. A PDB alone is not an HA guarantee.
- Review control-plane and data-path services by failure domain and client
  recovery, including CoreDNS/Cilium and admission webhooks, Loki reads, and the
  replicated Agentplane staging services. A streaming or stateful service needs
  evidence for its own reconnect, leader handoff, admission, or data recovery;
  two Ready Pods alone are insufficient. Keep singleton services in `Initial`.
- Continue admission-only coverage in parallel with HA preparation and observe
  completed cohorts together. Do not block low-risk sizing on an unrelated HA
  refactor or on an unrelated cluster rollout.

### 3. Cover exceptions with the right owner

- Review `Off` or uncovered operator, DaemonSet, storage, batch and ephemeral
  workloads with their actual controllers. Preserve operator-managed templates,
  persistence and per-node availability; an observed Pod does not imply its
  operator supports VPA. Coordinate storage changes with the existing storage
  work rather than starting a competing migration.
- Set static requests where VPA is unsuitable. For compatible workloads, choose
  `Initial` or a tested automatic mode without assuming a namespace label will
  safely cover dynamic Sandboxes, operator-created Pods, or short Jobs.
- Evaluate VPA `InPlace` for workloads where eviction is undesirable. VPA 1.7.1
  exposes it behind an alpha feature gate. Test admission, resize and kubelet
  restart behavior, capacity, and rollback before relying on it; `InPlaceOrRecreate`
  permits eviction. Do not treat either mode as permission to restart ntfy.

### 4. Simplify policy authoring in parallel

- Use and improve [`VpaProfile` and `ContainerPolicy`](../cdk8s/namespaces.py)
  so a namespace or workload expresses mode, controlled resources/values and
  justified bounds without repeated JSON. Add a concise CPU-and-memory
  requests-only path where it removes real duplication; keep explicit per-container
  exceptions and existing requests-and-limits behavior expressible.
- A workload's Goldilocks resource-policy annotation **replaces** its namespace
  policy. Build the complete effective policy at cdk8s time and verify inheritance,
  sidecars and `Off` entries; do not rely on an assumed runtime merge. Keep one
  owner for each VPA.
- Goldilocks can miss a Deployment-only annotation update. Follow the
  [reconciliation procedure](../docs/troubleshooting.md#goldilocks-vpa-stays-stale-after-a-deployment-annotation-change)
  and check all namespace effects before triggering it. Prefer a simpler upstream
  or declarative fix if one becomes available; do not build a custom scanner to
  retire Kyverno. Retain Kyverno where it is the simpler owner.

## Wave acceptance and completion

For each cohort, check current recommendations and their history, actual Pod
requests/limits and HPA metrics, startup floors, limit headroom, eligible-node
capacity and surge overlap. Bounds must be schedulable without hiding sustained
pressure; compare capped and uncapped recommendations and investigate sustained
cap hits. VPA cannot ensure that an evicted Pod can be scheduled or that a
workload recovers correctly.

For `Recreate`, verify replica placement, actual endpoints and service-specific
recovery, updater minimum replicas, PDBs and capacity. A Deployment rollout
and an eviction have different disruption controls. For `Initial`, verify
admitted resources on natural replacements or safe owner-matched admission dry
runs; do not restart a singleton merely to prove sizing. Verify Goldilocks'
effective VPA mode and policy after Flux applies changes, and check whether any
unexpected eviction, OOM, unschedulable Pod, or loss of service occurred.

Update the dashboard's dated snapshot, inventory, unresolved gates and next
cohorts as waves land; remove completed narration. The destination is reached
when compatible workloads have useful CPU and memory request sizing, safe
replicated services use automatic updates, each exception has an owner and
reason, and the dashboard makes missing requests, policy drift, unsafe
recommendations and rollout blockers actionable.
