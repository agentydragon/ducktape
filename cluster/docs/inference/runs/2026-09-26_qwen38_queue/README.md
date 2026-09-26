# One real task with natural compaction after the downloads

September 26, 2026. The user approved replacing the initial uncompacted Mini-SWE
queue, then directed us to observe compaction during a real eval task rather than
build a synthetic prerequisite. The initial queue source is retained in commit
83f4894913; this revision runs Terminus-2. No model-quality result is claimed yet.
Ollama pause PR [8070](https://github.com/agentydragon/ducktape/pull/8070) is merged;
live replicas were verified zero. No reboot or NixOS activation is involved.

## Order and protocol

1. Continue Q5_K_XL and IQ4_XS downloads in a separate low-priority service,
   following the [capacity recipe](../2026-09-26_qwen38_capacity/README.md). Downloads
   and checks remain serial, at 40 MiB/s with a 128 GiB free-space floor.
2. Meanwhile load the existing Q4_K_XL at 131,072 context, Q8_0 K/V, both GPUs, one slot.
3. Run the first task in the outcome-independent
   [CPU12 selection](../2026-09-26_harbor_review/cpu12_tasks.json),
   `interleaved-vigenere`, with Terminus-2 summarization enabled.
4. Stop the server and retain artifacts. The download service never starts inference;
   downloaded IQ4_XS will not launch a competing task.

The user authorized useful work on the already available model while downloading.
Q4 is the model from their successful OpenCode trial, avoiding a simultaneous change
of agent and weight quant. Record overlapping SSD traffic as a latency confounder;
this is not a clean speed comparison. Native 256K, smaller KV and matched quant runs
follow evidence from this first trajectory. Port 19080 is separate from user 18080.

Harbor is 0.23.0, with installed Terminus-2 reporting 2.0.0. Pin/hash the installed
adapter and backend source; preserve the generated job and Harbor resolved lock.
The model uses xhigh reasoning, temperature 0.6, a 32,768-token output cap, and a
3,600-second per-request timeout. Maximum agent turns: 500. No whole-task retries;
the installed agent/backend may retry individual calls according to their code.
Only the local endpoint and dummy key are supplied. Host-side Terminus uses
`http://127.0.0.1:19080/v1`; the task has no model credentials to a paid endpoint.

`model_info.max_input_tokens=98,304` reserves 32K of the physical window for output.
`proactive_summarization_threshold=32,768` starts summarization after the estimated
retained history passes 65,536 tokens, leaving additional room for the summary/Q&A
calls. These are deliberately conservative local settings, not AA's protocol.
The estimator can use a proxy tokenizer; actual server usage and overflow behavior
must be inspected. Summary, question, and answer requests are serial. Neither a
configured switch nor an incremented internal counter proves successful compaction.

Inspect main `trajectory.json` system steps with
`extra.context_management.type == "compaction"`, their saved subagent trajectories,
actual prompt-token changes, subsequent tool calls, and the task verifier result.
`store_all_messages` retains the final active history, not every pre-compaction
request; the main and summary trajectories provide the earlier evidence. A run
that finishes without compaction is still a valid task observation, but provides no
compaction evidence. Do not require two events before accepting a real task result.

## Resource and stopping rules

Q4 server cap: 34 GiB RAM with no extra swap; admission requires 58 GiB available
host RAM (server cap, two original 4 GiB task/verifier containers, 16 GiB desktop
reserve). Optional later IQ4 mode uses 24 GiB and requires 48 GiB available. GPU fit targets reserve 8 GiB on the desktop GPU and 2 GiB on the second.
Every 15 seconds during the attempt, check at least 16 GiB available host RAM, 6 GiB
free desktop VRAM, 1 GiB free second-GPU VRAM, and Ollama paused. A failed check stops
owned work and records a resource/service interruption, not a model-quality failure.
These guards reduce contention risk; they do not prove unaffected desktop latency.

Admission has a 24-hour limit from queue start; the user service has a 48-hour overall
ceiling, low CPU/I/O priority, and no automatic restart. Original real-task limits
remain: 28,800-second agent deadline, 900-second build/verifier deadlines, 4 CPU and
4 GiB per environment. A task may continue beyond the six-hour reporting checkpoint.

## Launch, observe and stop

From the Nix devshell, after stopping any previous writer of these partial downloads:

```bash
bash cluster/docs/inference/runs/2026-09-26_qwen38_queue/launch.sh /tmp/wyrm2-qwen38-downloads-20260926 downloads
bash cluster/docs/inference/runs/2026-09-26_qwen38_queue/launch.sh /tmp/wyrm2-qwen38-q4-20260926 q4
systemctl --user status wyrm2-qwen38-serial-queue
journalctl --user -u wyrm2-qwen38-serial-queue -f
systemctl --user stop wyrm2-qwen38-serial-queue
```

The launcher copies scripts/manifests and records revision and hashes, so later edits
do not mutate running code. Separate locks exclude duplicate inference and download
writers. Inference mode never downloads; download mode never starts a server. Artifacts are private to
the user in the supplied directory. Stop preserves verified and partial downloads.
Cleanup is limited to the server's unique queue label and compose projects named by
this invocation's unique trial configs. `harbor_attempt.sh --help` is safe;
`--config-only` writes configuration without starting a job, and `--install-only`
prepares the actual task environment without model calls or verification.

## Setup checks

A read-only inspection of the pinned task environment found bash and apt-get but no
tmux. Harbor's install-only run completed successfully in 16 seconds, with environment
and agent setup timestamps, no exception, no agent/verifier result, and no leftover
container. Evidence: `/tmp/terminus-real-task-install-20260926/jobs/attempt/`.
This checks infrastructure readiness; it is not a model/compaction test.
Shell syntax, Harbor JobConfig and Terminus2Options schema validation passed before
launch. The [compaction research](../2026-09-26_harbor_review/COMPACTION.md) records
source evidence and the tokenizer caveat. Practical OpenCode/repo tasks complement
this bounded benchmark; there is no full-suite score or model-equivalence claim.

## RAM cleanup and preflight incident

Seven idle Bazel servers were stopped using `bazelisk --noblock_for_lock shutdown`,
without deleting worktrees/caches or interrupting builds. The pause-PR worker then
stopped its own server. MemAvailable rose from approximately 35 to 55 GiB; it varies
with desktop work and other agents. Q4's 58 GiB threshold may require another 3 GiB.

A preflight helper accidentally called its `run.sh --help`; that wrapper ignored
arguments and launched an initial IQ4_XS Harbor job. The environment installed
Mini-SWE and started it, but port 19080 had no server. There is no successful model
response evidence (null token counters, no trajectory); a failed connection attempt
cannot be excluded. The exact owned container was stopped (exit 137), and checks
found no remaining matching container/network or Harbor process. This was not a
safe dry run or a benchmark observation. Validate configs without executing wrappers.

## Ollama evidence and follow-up

PR [8041](https://github.com/agentydragon/ducktape/pull/8041) records working tool and
thinking parsing, HDD cold loading around 708 seconds, and 0.14–1.44 tokens/s under
Ollama 0.34.0. It identifies an older vendored llama.cpp and competing embedding
traffic. The SSD reference used a newer runtime too, so storage and runtime effects
are confounded. Neither the old runtime nor HDD is yet isolated as the dominant
cause. A cgroup below its memory limit alone does not rule out file-backed I/O stalls.
A later Ollama comparison should hold model bytes, SSD backing, placement, context,
KV type and workload constant and record actual reads/faults and engine revision.
This queue first establishes useful capacity on the known SSD/runtime combination.
