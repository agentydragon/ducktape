# Qwen3.8 Flash Next IQ4: passing solution at the eight-hour deadline

September 26, 2026, wyrm2; inspected September 27. **The final solution passed all
six verifier tests, reward 1.0, but the agent reached its eight-hour deadline while
still testing.** Harbor records `AgentTimeoutError` alongside the reward. Three
natural compactions completed and tool use resumed after each. This is one attempt
on the same predetermined `interleaved-vigenere` task as the
[Q4 control](../2026-09-26_qwen38_q4_terminus_result/README.md), not a suite score or
proof of equivalent quant quality.

## Configuration and provenance

The [queue recipe](../2026-09-26_qwen38_queue/README.md) ran from immutable copied
revision `6411aede31e98e0087ac3d78caf775b603f1fd01`: UD-IQ4_XS on SSD, two RTX 5090s,
131,072 context, Q8_0 K/V, one slot, xhigh reasoning, temperature 0.6 and a 32,768
output cap. Harbor 0.23.0 / Terminus-2 2.0.0 and llama.cpp build 11151
(`bd4f514db14d87fded667787a7a963bfbaa98e89`) match the Q4 control. The original task
limits remained eight hours for the agent, fifteen minutes for verification,
4 CPUs and 4 GiB per environment. No concurrent model inference ran.

The later [settings audit](../2026-09-26_qwen38_queue/SETTINGS_AUDIT.md) applies to
both runs: effective min-p 0.05 and discarded previous internal reasoning, alongside
the temperature above. Neither those settings nor the later wrapper changes were
injected into this running attempt. Successful shutdown here does not validate the
new wrapper under the earlier Kubernetes failure condition.

[metrics.json](metrics.json) contains exact timings, token totals, compaction
boundaries and hashes of source artifacts. [Verifier output](verifier-stdout.txt)
and [CTRF evidence](verifier-ctrf.json) are committed. Raw logs, resolved configs,
main trajectory, nine summarization trajectories and generated artifacts remain in
`/tmp/wyrm2-qwen38-iq4-20260926/`; a private compressed copy is retained at
`/var/lib/llm-models-ssd/experiment-artifacts/wyrm2-qwen38-iq4-20260926.tar.gz`.
Archive SHA256: `feeb0668b5e252b7a8995ed3cfbb79471e8d268b3e3bb5ad99270fed4a43ae8b`.
Keep solutions and traces out of future benchmark agents' inputs.

## Outcome and comparison

Times are Pacific on September 26. Agent execution was 12:11:39–20:11:39;
verification finished at 20:12:45 and the queue completed at 20:13:03. The user
service subsequently reported inactive/dead, result success and exit status 0;
no Docker containers remained at the September 27 inspection.

| Measurement               |   Q4_K_XL control |             IQ4_XS |
| ------------------------- | ----------------: | -----------------: |
| Verifier tests / reward   |         6/6 / 1.0 |          6/6 / 1.0 |
| Agent execution           |        7h 39m 40s |  8h, agent timeout |
| Successful compactions    |                 2 |                  3 |
| Aggregate native decode   |    24.74 tokens/s |     40.79 tokens/s |
| Aggregate native prefill  |   125.07 tokens/s |    431.94 tokens/s |
| Native generated tokens   |           483,542 |            678,251 |
| Native completed requests |               161 |                266 |
| Native inference time     | 6h 16m 5s (81.8%) | 4h 57m 23s (62.0%) |
| Remaining wall time       |        1h 23m 35s |          3h 2m 37s |
| Bash command calls        |               246 |                418 |

Native timings sum the server's `prompt eval time` and `eval time` lines separately;
`eval time` must not also match `prompt eval time`. Throughput divides summed tokens
by summed seconds. Residual wall time includes commands, terminal waits and harness
overhead; it is not measured command CPU time. IQ4 requested command waits total
10,860.6 seconds. Its 256 recorded agent steps differ from Harbor's 257 episode
counter; neither should be mistaken for the 266 native requests including summaries.
Harbor reports 10,493,692 input, 9,969,208 cached-input and 678,251 output tokens.
Repeated history contributes to input totals; these are not simultaneous context.
No completed native decode hit the 32,768-token cap, unlike one Q4 request.

IQ4's observed decode was 1.65 times Q4's and prefill 3.45 times Q4's, but this is
not an isolated weight-quant experiment: IQ4 used a 24 GiB server RAM cap versus
34 GiB for Q4, different CPU-mapped working sets, different trajectories, and no
model downloads overlapping its run. Q4's first hour overlapped downloads. Both
configurations still use CPU-mapped tensors/lazy embeddings, not fully GPU-resident
weights. Faster inference did not produce earlier agent completion in this pair.

## Compaction and timeout evidence

| Completed handoff | Main step | Previous action prompt | Resumed action prompt |
| ----------------- | --------: | ---------------------: | --------------------: |
| 14:14:08          |        35 |                 69,096 |                 6,501 |
| 16:14:27          |       118 |                 72,392 |                10,778 |
| 18:37:07          |       208 |                 71,867 |                 7,100 |

Each main-trajectory system marker records `context_management.type=compaction`
and `boundary=replace`; all three have saved summary/question/answer trajectories
and subsequent ordinary tool use. The table compares adjacent ordinary action
requests, not identical payloads before and after compression. No context-overflow
exception was recorded.

At timeout the agent was polling its own additional out-of-space regression suite.
The traceback ends inside Terminus's terminal-wait sleep, not an LLM request or
summarization call. The final saved action says the sample remains byte-identical
and another synthetic case now decodes correctly, then requests another poll. These
are trajectory observations, not independent proof of earlier hidden-test success.
Harbor then verified the saved solution in its separate verifier environment:
6 passed in 42.65 seconds. The timeout is real deadline exhaustion, not a failed
compaction or the post-result guard failure seen in Q4. Reward and completion
status must both remain visible in reports.

## Next experiments

1. Audit the late trajectory for useful fixes versus repeated testing and how the
   harness communicates remaining time. Do not assume the final solution would have
   passed hours earlier, or extend the deadline just because the agent used it all.
2. Keep IQ4 as a promising candidate and run a different predetermined task serially
   to broaden evidence. Preserve original deadlines/verifiers and record whether the
   agent finishes voluntarily as well as the final reward.
3. Follow the settings audit with separate sampling and reasoning-history comparisons;
   label changes explicitly rather than pooling them with this original pair. Validate
   actual next-request reasoning retention and compaction behavior before a long run.
4. Defer a full 66-task suite. Smaller KV, larger context, Q5 and IQ3 remain subsequent
   capacity/quality experiments; this one shared task cannot rank their capability.

No follow-up inference was automatically queued or launched as part of recording
this result.
