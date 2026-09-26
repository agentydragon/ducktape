# Serial work after the quant downloads

Prepared September 26, 2026. This is a bounded local experiment, not an Artificial
Analysis replication. The scripts have not yet completed inference validation.
The user authorized downloads, GPU experiments, model SSD use, and pausing Ollama;
PR [8070](https://github.com/agentydragon/ducktape/pull/8070) is merged and live
Ollama replicas were verified zero. No reboot or NixOS activation is involved.

## Frozen order

1. Resume and SHA256-verify the pinned Q5_K_XL and IQ4_XS downloads from the
   [capacity recipe](../2026-09-26_qwen38_capacity/README.md). Downloads and hash
   checks are serial, rate limited to 40 MiB/s, with a 128 GiB SSD free-space floor.
2. IQ4_XS: 128K allocation/retrieval probes with Q8_0, Q5_0, then Q4_0 KV.
3. IQ4_XS: native 256K with Q8_0 KV; after successful retrieval, one Terminal-Bench
   attempt on `interleaved-vigenere`, the first task in the outcome-independent
   [CPU12 selection](../2026-09-26_harbor_review/cpu12_tasks.json).
4. Repeat steps 2–3 with the existing Q4_K_XL weights.

The smaller weight format goes first because its host-memory requirement is lower,
not because of observed task outcomes. Q5 is downloaded as a later quality control;
its larger hot weight set makes it a poor first choice for this host's remaining RAM.
Each run uses both GPUs for one model, one server slot, and one request at a time.
No parallel inference is scheduled. Port 19080 is separate from the user's 18080.

The 128K probes contain 120K filler tokens; the 256K probe contains 240K. Three
fixed facts are inserted at separated positions and checked in the answer. This
measures admission and simple retrieval, not agent quality or adequate long-context
reasoning. Requests disable thinking for retrieval and retain complete payloads,
responses, startup logs, effective server properties and GPU/RAM snapshots.
Failure of the 256K probe stops the queue before a quality attempt.

## Resource and stopping rules

Server cgroup caps are 24 GiB RAM for IQ4_XS and 34 GiB for Q4, with no extra swap.
Admission requires respectively 48 and 58 GiB MemAvailable: server cap plus two
original 4 GiB task/verifier containers plus 16 GiB desktop reserve. Fit targets
reserve 8 GiB on the desktop GPU and 2 GiB on the second GPU. Every 15 seconds during
requests, the queue checks for at least 16 GiB available host RAM, 6 GiB free desktop
VRAM, 1 GiB free second-GPU VRAM, and Ollama still paused. A failed check stops only
this queue's inference and records a resource/service interruption, not a model fail.
These checks reduce contention risk; they do not prove desktop latency is unaffected.

Admission waits at most 24 hours from queue start. The transient user service has a
48-hour overall ceiling; interruption is not a benchmark timeout. It has low CPU/I/O
priority. No automatic restart or inference retry is configured. Original task limits
remain: 28,800-second agent deadline, 900-second build/verifier deadlines, 4 CPU and
4 GiB per environment. No task starts before the admission checks pass.

Harbor 0.23.0 uses Mini-SWE-Agent 2.4.6, step limit 500, output cap 32,768 tokens,
zero retries and one task per invocation. The pinned Mini-SWE default agent has no
compaction. Preserve context overflows as their own termination category. This cap
and subset define a local protocol, not an AA score. Reasoning is set to `xhigh` in
the server template; Harbor's `reasoning_effort` is deliberately unset because it
switches the adapter to Responses. Only the local API URL and dummy key are supplied.
The installed Harbor adapter sets a 3,600-second individual model-request timeout;
the task deadline remains the outer bound. Installed adapter/config hashes are
retained alongside the version, and Harbor retains its resolved job lock/config.
A single task per quant cannot estimate model equivalence or a reliable pass rate.

## Launch, observe and stop

From the Nix devshell, after ensuring no earlier download process is writing these
same `.partial` files:

```bash
bash cluster/docs/inference/runs/2026-09-26_qwen38_queue/launch.sh /tmp/wyrm2-qwen38-queue-20260926
systemctl --user status wyrm2-qwen38-serial-queue
journalctl --user -u wyrm2-qwen38-serial-queue -f
systemctl --user stop wyrm2-qwen38-serial-queue
```

`launch.sh` copies scripts/manifests and records their hashes and source revision
before launching. Subsequent edits do not alter that running copy. A runtime lock
excludes a second queue. Output is private to the user, under the supplied directory;
retain it for analysis and commit a result summary after reviewing for sensitive
trajectory content. Stopping preserves verified and partial model downloads.
Container cleanup is limited to the server's unique queue label and task compose
projects whose unique trial names are recorded in this invocation's artifacts.

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
