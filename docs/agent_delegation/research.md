# Delegation research report

Reviewed 2026-09-24. [Index](README.md) · [Task evidence](task_evidence.md) ·
[Reusable skills](reusable_skills.md).

## The actual optimization problem

Choose task decomposition, worker model, reasoning effort, verification strategy,
and escalation together. Minimize expected total resource cost subject to an
acceptable probability of accepting incorrect work. API dollars, elapsed time, and
operator attention are separate quantities; converting them into one objective
requires explicit weights, not adding seconds to dollars.

For one cheap attempt with a stronger fallback, a useful accounting sketch is:

```text
expected cost = dispatch + worker + verification
              + P(recovery needed) * recovery cost
```

Recovery includes diagnosis, context transfer, repair, re-verification, and integration.
An undetected error is not a successful cheap completion. There are no calibrated
probabilities for this repo yet; the equation identifies what to consider, not
numbers an agent should invent at every dispatch.

The practical comparison is against completing the same task with the parent or a
stronger worker, including that alternative's verification. A stronger model is not
an oracle. Nor must a cheaper worker be equally reliable on its first attempt: a
cheap, checkable attempt can win after accounting for occasional recovery.

### Verification is a design choice

A typed manifest conversion can take substantial exploration yet admit a compact
semantic comparison of output resources. A three-line concurrency change can require
a difficult correctness argument. Authored diff size therefore predicts reading
volume better than either generation tokens or verification difficulty. Reasoning
and discarded approaches do not appear in the diff.

Reading a manageable authored diff plus affected callers, independently chosen
invariants, and actual check results is often cheaper than recreating the work.
Generated output needs semantic comparison rather than thousands of lines of visual
review. Tests authored alongside the implementation may encode the same mistaken
assumption. Independent review means checking the claim against another source of
truth; it does not necessarily mean paying a second model to read everything.

## Dated model costs and eval anchors

Source of truth: [AA snapshot and methodology](../artificial_analysis/README.md),
[full-precision selected rows](../artificial_analysis/cited_models_2026_09_24.csv).
Artificial Analysis, 2026-09-24, Intelligence Index v4.3.2. Rounded here:

| GPT-6 model / effort | Index | Index cost/task | Cost / Luna xhigh | Terminal-Bench 4.0 |
| -------------------- | ----: | --------------: | ----------------: | -----------------: |
| Luna high            | 32.15 |        $0.02862 |             0.69x |              4.55% |
| Luna xhigh           | 33.88 |        $0.04171 |                1x |              8.08% |
| Luna max             | 37.26 |        $0.06809 |             1.63x |             12.63% |
| Sol low              | 33.90 |        $0.13224 |             3.17x |              9.09% |
| Sol medium           | 39.78 |        $0.24820 |             5.95x |             18.69% |
| Sol high             | 42.82 |        $0.37463 |             8.98x |             26.26% |
| Astra low            | 45.78 |        $0.81751 |            19.60x |             41.92% |
| Astra high           | 50.92 |        $1.72525 |            41.36x |             54.04% |

These costs cover the whole weighted index task mix, **not Terminal-Bench costs**.
Dividing the adjacent cost by the Terminal-Bench score would produce a meaningless
cost-per-success estimate. Neither column measures subscription allowance usage.
Luna max is cheaper than Sol low while scoring higher on both displayed quality
metrics: model size and effort need to be chosen jointly, not by a size-only ladder.

Inference: the price gaps leave room for targeted parent checking and occasional
repair. They do not establish how much verification fits into a particular repo
task's budget. The owner reports Luna handles substantial work, not merely clerical
edits; that is useful local qualitative evidence, not a repeated-run measurement.

## Agentic research: closest matches first

### Applied Compute: Training an Agentic Router

[Report](https://www.appliedcompute.com/research/training-an-agentic-router).
497 SWE-bench Verified tasks, three rollouts per candidate, including Nemotron 3
Ultra, Opus 4.7, and GPT-5.5. A trained router reportedly reaches roughly 76% on
held-out tasks at about 25% lower cost than GPT-5.5. The 89% oracle point uses known
outcomes and is not deployable performance.

Especially relevant: the report constructs routing guidance from task-level
outcomes, repository inspection, and cost-adjusted differences, and rejects some
intuitive predictors such as issue length. Its examples include a localized Astropy
property/docstring bug solved by the inexpensive model and Django permission work
routed successfully to Opus. Another ORM example overpays for Opus despite the
cheaper model also succeeding.

Borrow the method of identifying recurring, observable task features and negative
signals. Do not copy its model mapping onto GPT-6. Three trials give noisy per-task
estimates; selected examples are not a complete success-rate matrix. Parent
verification economics are not established by this report.

### Agent-as-a-Router / CodeRouterBench

[Paper, June 2026](https://arxiv.org/html/2606.22902v1) ·
[Code and benchmark](https://github.com/LanceZPF/agent-as-a-router).
Eight candidate models, roughly 10,000 tasks, with 176 out-of-distribution agentic
tasks in addition to the much larger coding-task set. Giving a router dimension-level
performance statistics raises its score from 41.41 to 47.74 (15.3% relative).
The proposed loop incorporates execution-grounded verification and experience.

Borrow empirical task profiles and learning from observed failures. The reported
gain is not itself a cost saving, and most tasks are not long-horizon agent work.
Verifier reliability and cost weights matter; the evidence does not identify the
best GPT-6 worker/parent combination. Appendix D provides model-by-dimension tables;
these should not be mistaken for a model-by-individual-task matrix.

### SkillOrchestra

[Paper, February 2026](https://arxiv.org/abs/2602.19672) ·
[Implementation](https://github.com/jiayuww/SkillOrchestra).
Learns skill-specific agent competence and cost from execution experience and routes
as requirements evolve. The abstract reports up to 22.5% improvement over compared
RL orchestrators and 700x/300x reductions in **learning cost**, not those factors in
runtime inference savings.

Borrow fine-grained capability profiles rather than a universal model ranking.
Here “skills” are learned routing knowledge, not a drop-in Codex `SKILL.md`.
A trained orchestration system is substantially more machinery than this repo's
current need for lightweight delegation instructions.

### Reasoning effort and first-try reliability

[Observational study, September 2026 revision](https://arxiv.org/abs/2607.02436v2).
Ninety runs build one retrospective-board application. Reported high-to-xhigh
comparisons improve perfect first-try completion from 28% to 89% for 9–29% more cost;
adding a browser-testing tool costs 42–68% more without the measured functional gain.

Borrow measuring complete first-try success and repair effort, not only averaged
rubric points. This is one application with several configurations and observational
comparisons, not a controlled estimate of Luna xhigh's advantage. It is not grounds
to remove browser tests or this repo's required verification.

### Scaling Test-Time Compute for Agentic Coding

[Paper, April 2026](https://arxiv.org/abs/2604.16529).
Uses structured trajectory summaries for selecting and refining attempts. Reports
Opus 4.5 improving from 70.9% to 77.6% on SWE-bench Verified and 46.9% to 59.1% on
Terminal-Bench 2.0 under the named harnesses.

Borrow concise handoffs preserving hypotheses, progress, and failed approaches.
Improved quality from extra attempts is not evidence of lower total cost. Routine
ensembles or tournament reviewers are not justified by these results alone.

### Anthropic's multi-agent research system

[Engineering report, June 2025](https://www.anthropic.com/engineering/multi-agent-research-system).
Describes an orchestrator issuing bounded research assignments with explicit
objectives, outputs, sources, and limits. It reports multi-agent systems using about
15x the tokens of chats; this is not a matched coding-task cost comparison.

Borrow clear contracts and scaling effort to the question. Parallel research can
reduce latency while increasing total spend. Neither the architecture nor its
reported research gains establish a default number of agents for coding tasks.

## Earlier work: useful background, weaker transfer

- [RouteLLM (2024)](https://arxiv.org/abs/2406.18665): preference-based routing on
  older models and largely single-turn evaluations. Background on routing, not
  evidence for modern tool-using coding-agent assignments.
- [FrugalGPT (2023)](https://arxiv.org/abs/2305.05176): cost-aware cascades illustrate
  why the acceptance mechanism matters. Its savings do not transfer to our workload.
- [Scaling test-time compute optimally (2024)](https://arxiv.org/abs/2408.03314):
  difficulty-dependent reasoning budgets, primarily mathematical reasoning; not
  a direct measurement of repository work with parent review.
- [AI Agents That Matter (2024)](https://arxiv.org/abs/2407.01502): methodological
  case for joint cost/accuracy evaluation, held-out testing, and reproducibility,
  rather than an operational model-selection skill.

## Concrete assignments: hypotheses, not promises

The following are proposed initial allocations. None has a measured ducktape
first-pass reliability rate. Public repeated-run examples live in
[task_evidence.md](task_evidence.md), with their actual model generations.

| Task and supplied context                                                       | Starting worker                                             | Acceptance / reason to spend more                                                                                                   |
| ------------------------------------------------------------------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Inventory raw YAML resource kinds and source ownership, with defined exclusions | Luna low/medium, or direct tools if trivial                 | Reconcile totals with an independent search; sample classifications                                                                 |
| Migrate a stateless workload to an existing cdk8s construct pattern             | Luna xhigh                                                  | Compare resource identities/specs, generated output, and affected targets; escalate unresolved ownership semantics, not typing work |
| Implement a clear feature through model, API, and tests using existing patterns | Luna xhigh; max if reasoning demands it                     | Contract tests plus integration behavior and caller review; multiple files alone do not require Sol                                 |
| Investigate an intermittent reconciliation failure with conflicting evidence    | Sol medium/high when no bounded Luna assignment captures it | Reproducer and causal evidence; a separate bounded log inventory can still use Luna                                                 |
| Choose a stateful ownership handoff or review a subtle authorization boundary   | Strong parent or Astra at justified effort                  | Independent invariant/safety argument and required live evidence; cheaper implementation can follow a settled plan                  |

### Harness applicability

The operational policy is capability-gated. Our Codex session exposes worker model
and effort controls; its full-history fork mode inherits the parent's settings,
while explicit overrides require no fork or a bounded history fork. This is a
session tool contract, not a claim about every Codex deployment.
[Official Codex documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents)
also documents explicit model/effort selection and inheritance. Its general defaults
are not the same as this owner's cost-conscious Luna xhigh policy.

For Claude sessions without those controls, the task, verification, and recovery
principles remain useful, but the GPT model choices are inapplicable. No unsupported
override or guessed cross-provider tier mapping is required.

## What would materially improve this policy

1. Obtain GPT-6 model-by-task repeated-run results under a comparable harness. The
   concrete public chart inspected here is GPT-5.6, not GPT-6.
2. Observe ordinary delegated work: task shape, model/effort/harness, exact revision,
   acceptance checks, first-pass result, repair needed, and worker plus parent cost
   where available. Preserve failures as well as successes in normal task reports.
3. Compare policies on held-out task families. Avoid selecting examples after seeing
   who passed, then treating those examples as predictive evidence.
4. Measure verifier misses as well as rejected outputs: passing worker-authored tests
   alone is not evidence that parent checking caught the important mistakes.

These are evidence gaps, not a proposal to build a routing service or run an expensive
benchmark suite before the next delegation. A quick, revisable default is enough
until observed costs or failures justify more machinery.
