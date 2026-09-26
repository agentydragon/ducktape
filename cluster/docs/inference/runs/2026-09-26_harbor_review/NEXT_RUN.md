# Next local quality run: protocol, not results

Status: initial protocol, superseded for the immediate run by the
[serial download/capacity/task queue](../2026-09-26_qwen38_queue/README.md).
That queue starts with IQ4_XS then Q4 because host-memory fit is now the first
question; Q5 remains a later quality control. Its record documents the accidental
preflight environment launch. Statements below describe the earlier planning state.

## Question

Does the useful local Flash-Next configuration retain enough hard-task capability
for regular agent work, and does Q5 improve on Q4? A small Terminal-Bench subset can
expose operational failure and large quality differences. It cannot establish
GPT/Claude equivalence or precisely estimate a full-suite pass rate.

## Before starting

- Pause Ollama through the authorized focused GitOps change, verify it released GPU
  memory, and establish that no interactive inference is using the experiment server.
  Do not interrupt desktop use, reboot, or activate NixOS.
- Pin the dataset commit, task content hashes, Harbor and agent versions, model
  revision/shard hashes, runtime image, template and complete effective config.
- Preflight CPU-only task environments without model calls. Preserve task resource
  requirements; do not give a GPU task a CPU environment and score it as equivalent.
- Inspect the installed agent's compaction implementation and effective settings.
  The AA-shaped Mini-SWE track has no compaction; record context overflow as such.
  A practical compaction track must instead verify a forced compaction and retain
  its summary plus resumed trajectory. Do not combine scores across those policies.
- Establish safe Q4 and Q5 placement at a common context and KV precision. Prefer
  native 256K if both fit with desktop headroom; otherwise declare the common lower
  context before evaluating. Keep Q8 KV initially. Allocation is not a quality test.
- Verify output reserve, step limit, reasoning setting and request/tool/task timeouts.
  Use the original task deadline, make the time stated to the agent agree with it,
  and capture the resolved values. Do not silently shorten deadlines to fit a schedule.

## Schedule and controls

**One task, one server slot, one active inference request at a time.** No concurrent
model configurations, background probes, or overlapping subagent inference. Use
Harbor concurrency 1 and llama.cpp `--parallel 1`; verify effective settings rather
than assuming defaults. Await/cancel outstanding requests before changing models.
The two GPUs may cooperate on this single model. They do not imply two eval workers.

The [frozen manifest](cpu12_tasks.json) selects 12 tasks from the 63 of 66
with no declared task/verifier GPU requirement. It records the dataset digest,
selection seed/rule, task digests and task.toml hashes. Selection did not use previous
outcomes. These declarations have been inspected; environments have not been launched.
Run pairs serially: task 1 Q4, task 1 Q5, task 2 Q5, task 2 Q4, continuing alternating
order. This costs reload time but yields matched observations early. Start with four
pairs; expand along the fixed list if budget permits. No replacing difficult tasks
with easier ones after seeing outcomes.

Reserve 24–48 hours total. At the declared cutoff, stop and record an interrupted
attempt as budget-censored, not a benchmark timeout or a completed model failure.
Report attempted, completed and paired counts; never promise all 24 attempts will
finish. Prefer not to start a task whose full allowed duration exceeds the remaining
budget. Unstarted tasks remain pending. Actual task deadlines are unchanged.

Hold runtime, base-model revision, prompt/template, reasoning, sampling, output and
step allowances, context and KV precision constant between weight quants. Record
placement differences required by Q5 and their latency/memory consequences. This
measures the usable local configuration, not pure numerical quantization error.
Reset task environments for each attempt, preserve logs, and record cold/warm cache
state. Do not compare one cached run with one cold run without identifying that cost.

## Results and decision

For every attempt retain verifier reward, wall time, token/cache counters, effective
context, trajectory and termination cause: success, task failure, context exhaustion,
upstream task timeout, global budget interruption, or infrastructure error. Keep the
full attempted denominator and paired task table; report completed-only figures only
with the missingness visible. Retries remain separate attempts with stated reasons.

Inspect failure trajectories to separate inability to solve a task from unproductive
loops, environment breakage and context loss. Any repeats selected because two quants
disagreed are diagnostic, not an unbiased revised headline score. Freeze a subsequent
confirmation cohort if the initial result would change the chosen configuration.

The OpenCode task crossing 128K cumulatively, compaction and four subagent calls is
already useful practical evidence. Preserve that workflow alongside the harder
benchmark. If both quants repeatedly exhaust context, investigate bounded history
management separately; do not spend the whole budget reproducing the same overflow.
After this bounded batch, give the next promising alternative a serial feasibility
pass instead of exhaustively tuning Qwen. Deployment integration remains secondary.

## Read-only preflight findings and remaining gates

Harbor 0.23.0's installed adapter accepts Mini-SWE config passthrough but exposes no
specific compaction switch. Subsequent pinned-source inspection confirmed that
Mini-SWE 2.4.6's default agent has no compaction and accepts `agent.step_limit`.

Setting Harbor's `reasoning_effort` for an `openai/...` model selects Mini-SWE's
`litellm_response` path and maps the output cap to `max_output_tokens`; without that
option the previous run used chat completions. Do not silently change API paths to
set reasoning. Verify a compatible configuration with the pinned llama.cpp server,
including the actual template reasoning setting, before a long run.

No launch command is claimed validated. Confirm the task selection accepted by this
CLI and preserve the dataset digest; do not assume repeated include flags compose.
Since the protocol runs one task per invocation, it need not depend on multi-filter
behavior. Confirm cancellation leaves no active model request and preserves trial
logs before enforcing an external budget. Safe 256K placement and Q5 loading are
still unmeasured. The later accidental Docker task launch is recorded in the queue
notes; it produced no successful model-response evidence.
