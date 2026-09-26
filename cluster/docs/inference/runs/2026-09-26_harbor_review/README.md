# Harbor follow-up: bounded review, September 26

This is a read-only review of the operator's local `jobs/` directory, not a new
evaluation run. It informs whether to reuse Harbor; repairing every failure is
not a prerequisite for the inference program. No jobs, containers or services were
changed by this review. The original logs remain outside Git in
`/home/agentydragon/code/ducktape/jobs/`.

## Successful OpenCode reference supplied by the operator

The operator reports that this model completed a long knowledge-management task
reasonably in OpenCode, with no obvious bugs or breakages. This is a real useful
agent outcome; it is not an independently scored coding benchmark. The concrete
OpenCode configuration supplied in the conversation is preserved in
[opencode.json](opencode.json). With the corresponding server running:

```bash
OPENCODE_CONFIG="$PWD/cluster/docs/inference/runs/2026-09-26_harbor_review/opencode.json" opencode
```

[operator_commands.txt](operator_commands.txt) preserves the user-referenced
`./qwen3.8-flash-docker-cmd` file as read on September 26. It names the same pinned
llama.cpp image and SSD checkpoint as our September 24 probe, with `--ctx-size 131072`, one slot and `--fit-target 1024,1024`. It also exposes the server on the
Docker bridge for Harbor. Compared with our measured 32K run it reduces the desktop
free-VRAM target from 8 GiB to 1 GiB and omits the container RAM/CPU limits. Preserve
this as an operator recipe, not proof that every recorded job used every flag or a
recommendation to reduce workstation headroom in future unattended experiments.

The supplied OpenCode config sets the provider/model/URL but no explicit context,
output, reasoning or compaction settings. Their effective values can also depend
on OpenCode version, defaults and merged configuration; capture those next time.
The file's alternate OpenCode example includes `parallelToolCalls: false`, while
the conversation's exact command does not. Keep that distinction instead of
assuming the successful session had this option. Parallel tool calls and concurrent
model-serving slots are separate settings.

## Latest saved run

`2026-09-25__01-06-09`, job ID `12fa69be-f9b3-49e0-af85-f0eb409e2548`, used
Terminal-Bench 4.0 and mini-swe-agent with `openai/qwen3.8-flash-next-q4`.
The saved result has no finish timestamp: 12 completed trials, 54 pending,
6 errors, 1 pass (`react-lead-form`) and mean 0.08333. This is a later snapshot
than the operator's 1/11 report. It is not a completed 66-task benchmark.

All six recorded `NonZeroAgentExitCodeError` exceptions end in
`litellm.ContextWindowExceededError`. The backend reports a 131,072-token limit:

| Task                      | Rejected request tokens |
| ------------------------- | ----------------------: |
| layout-config-recreation2 |                  131298 |
| biped-contact-dynamics    |                  131679 |
| lake-temp-glm             |                  132138 |
| takens-embedding-lean     |                  131408 |
| satb-audio-transcription  |                  131864 |
| fin-saccr-rwa             |                  132258 |

These errors are context exhaustion, not Harbor's total-task timeout. They do not
establish that a compactor ran and failed. Before another eval, check the installed
agent's compaction support/configuration, context trigger, output reserve and error
policy, and exercise it on a disposable short trajectory. Harbor orchestration and
the selected agent's history-management policy are different layers.

Installed Harbor is **0.23.0**; the representative trial records Mini-SWE **2.4.6**.
Harbor's `harbor/trial/trial.py` selects the task agent timeout unless overridden
(`_compute_agent_timeout_sec`, line 1436) and enforces it with `asyncio.wait_for`
(line 554). A provider exception can end the agent earlier than that deadline.
The representative saved Mini-SWE config has `wall_time_limit_seconds=0`; that is
not the enclosing Harbor deadline. Its 139-message saved trajectory has no
compaction marker, which is not proof of the worker's internal policy.

The installed Harbor context-error regex in `harbor/agents/installed/base.py:492`
recognizes other provider phrasings, but not the observed "request ... exceeds the
available context size" wording. That explains the unhelpful generic nonzero-exit
classification; recognizing the error would not itself provide compaction or
allow the run to continue. Source paths are relative to
`~/.local/share/uv/tools/harbor/lib/python3.12/site-packages/`, inspected September 26.

The saved aggregate counters are 36,399,090 input tokens, 35,719,420 cached input
tokens (~98.1%), and 1,326,739 output tokens. Summing the per-trial records
reproduces these aggregate counters. At the separately measured ~30 tokens/s,
the recorded output alone represents ~12.3 hours of decoding. This is illustrative
arithmetic, not a measured server-time breakdown: context-dependent speed varies,
and these harness counters have not been independently reconciled with server logs.
High reported cache reuse argues against simply assuming every turn re-prefills
all history. Long reasoning/action trajectories themselves deserve measurement.

Do not turn 1/12 into a model-only quality estimate, or turn 1/6 after dropping
errors into a corrected score. Report the failures and attempted denominator;
only a controlled rerun can show what those tasks would have done without overflow.

## Earlier runs and the GPU error

The two September 24 job directories are also incomplete. Their saved aggregate
results are dominated by `NetworkConnectionError`; the second also includes two
nonzero agent exits. These are not additional clean model-quality observations.

The operator's later `Task requires 1 GPU(s) ... DOCKER ... does not support GPU
allocation` traceback is an environment capability check. It is separate from
inference-server GPU access and from the six context errors above. Running the LLM
on host GPUs does not grant a Harbor task container its requested GPU. Do not
silently force GPU-dependent tasks to CPU and compare the result to a full-suite
leaderboard. A declared CPU-only subset can be useful for local screening.

## External comparison and next decision

[Selected AA rows](aa_selected_rows.json) were fetched from the public
[Artificial Analysis leaderboard](https://artificialanalysis.ai/leaderboards/models)
on September 26, 2026. Only the three comparison rows are retained. Qwen3.8-Flash-Next
has index 39.82 and Terminal-Bench 4.0 25.25%; Sol low has 33.90 / 9.09%, and Sol
medium 39.78 / 18.69%. These are external served-model measurements, not our local Q4.
The local user's modified AA snapshot files were read for orientation and left intact;
these rows were independently refreshed from the public source.

AA's [methodology](https://artificialanalysis.ai/methodology/intelligence-benchmarking)
specifies 66 tasks, three repeats per task, 500 agent steps, upstream task deadlines,
and **no context compaction or summarization** in mini-swe-agent. Adding compaction
may improve practical local agents, but changes that protocol. The aggregate index
also combines multiple evaluations, so it is not a Terminal-Bench pass percentage.

The next decision is a small clean screen, not another full suite. Preflight a
few CPU-only environments; verify tool roundtrips and context handling; then compare
matched tasks at explicit context, reasoning, output and wall-time budgets. Keep
infrastructure failure, context exhaustion and task failure separate. Detailed
context/concurrency/precision experiments are in [PLAN.md](../../PLAN.md).

## Neighboring model/effort comparison

Selected additional rows from the same September 26 AA public-page fetch, using
index v4.3.2 and Terminal-Bench 4.0. Raw selected fields are in
[aa_neighbours.json](aa_neighbours.json). Claude entries use adaptive reasoning;
Opus 5.5 low uses AA's default-fallback variant.

| Model / effort                        | Intelligence Index | Terminal-Bench 4.0 |
| ------------------------------------- | -----------------: | -----------------: |
| GPT-6 Luna max                        |              37.26 |             12.63% |
| Claude Sonnet 5 max                   |              38.16 |             14.14% |
| GPT-6 Sol medium                      |              39.78 |             18.69% |
| Qwen3.8-Flash-Next                    |              39.82 |             25.25% |
| Claude Opus 5.5 low, default fallback |              42.31 |             31.31% |
| GPT-6 Sol high                        |              42.82 |             26.26% |

This is model positioning from external evaluations; our local Q4 precision/runtime
is not independently scored by those rows. The relevant neighbor depends on the
metric. Context capacity and concurrency must be measured on wyrm2, while deployment
integration can follow once those more interesting questions are answered.
