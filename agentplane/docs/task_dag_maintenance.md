# Task DAG maintenance

`plans/task_dag.md` is the dispatch map for unfinished work, not a catalog
of possible improvements or an acceptance-history ledger. `plans/task_freezer.md`
holds deliberately deferred ideas with a concrete reason to revisit them. Component plans own
detailed designs; the DAG owns status and dependencies. Keep their links and decisions consistent
when changing either.

- **Nodes:** one independently finishable outcome each, with state, prerequisites and proportional
  exit evidence. Split larger plans at design, implementation and rollout boundaries; use a capstone
  only when downstream work needs the combined result. No node per test, no speculative
  implementation phases before a design is chosen.
- **Operator decisions are explicit nodes** naming the alternatives, the recommendation and the
  question for review. Downstream implementation waits on it; drafting alternatives may proceed in
  parallel. Never silently choose transport, authority or RBAC while rewriting plans.
- **Edges** are real prerequisites, including shared foundations and migration sequencing holds.
  Label technical dependencies separately from operational holds and conditional branches. An edge
  constrains starting mutating work where stated, not just final acceptance. Graph position implies
  no priority and no permission to start a blocked implementation.
- **In-flight work** records who/what reported it, with timestamp and scope. A partial backfill,
  merged implementation or passing unit test is not a completed rollout. Do not infer current state
  from an old log. Remove finished nodes; evidence goes in the owning component docs.
- **Temporary rollout holds, migration sequencing and task-specific exceptions** live in the DAG and
  component plans, never in `AGENTS.md`.
- **Acceptance** addresses the changed contract and credible risk. Keep security-denial, revocation,
  isolation, data-preservation and ordinary retry/reconnect/concurrency tests. Prefer deterministic
  automated coverage; require a live check only for an identified deployment-specific uncertainty,
  with a bounded scenario and stopping condition. Do not require observing a provider outage in
  production, repeating operator-accepted work, or compound-disaster drills by default.
- **Missing live evidence alone is not unfinished work** when suitable automated evidence exists.
  Remove redundant or waived gates rather than freezing them as mandatory chores. Unlikely hardening
  without a concrete need is omitted, or frozen with an incident/measurement/product-decision
  trigger. Frozen items neither block the DAG nor imply priority.
- **Before publishing a DAG refactor**, check nodes/edges, cycles, linked anchors and duplicated
  outcomes. Explain material gate removals; never drop unresolved security or migration risks as
  “over-conservative” to shorten the list.
