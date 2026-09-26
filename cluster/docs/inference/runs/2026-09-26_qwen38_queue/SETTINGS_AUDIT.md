# Inference settings audit, September 26

The completed Q4 and active IQ4 runs use the same agent/sampling settings. This audit
does not change either run. Evidence: copied launch/job configs, saved `/props`,
native sampler logs, served chat template, installed Harbor source and Q4's saved
message history. The [Qwen model card](https://huggingface.co/Qwen/Qwen3.8-Flash-Next#best-practices)
and [Unsloth guide](https://unsloth.ai/docs/models/qwen3.8-next#recommended-settings)
were checked on September 26.

## Effective behavior

| Setting                                   | Observed configuration                                 | Assessment                                                                                              |
| ----------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| Temperature                               | 0.6 in Harbor requests/native logs; server default 1.0 | Qwen recommends 1.0 for thinking. Server defaults alone miss the override.                              |
| Min-p                                     | 0.05 in defaults and native logs                       | Qwen recommends 0.0; the inherited runtime default filters candidates more aggressively.                |
| Top-p / top-k                             | 0.95 / 20                                              | Matches guidance.                                                                                       |
| Repetition / presence / frequency penalty | 1.0 / 0 / 0                                            | No extra repetition penalty; matches thinking guidance where specified.                                 |
| Thinking / effort                         | Enabled / xhigh in server and request kwargs           | Supported; responses contain reasoning.                                                                 |
| Preserve thinking, template               | Defaults true when unspecified                         | Client must supply prior reasoning.                                                                     |
| Preserve thinking, Harbor                 | `interleaved_thinking` omitted, defaults false         | Prior internal reasoning is discarded from subsequent requests.                                         |
| Context / slots                           | 131,072 / 1                                            | Actual allocation matches the serial experiment.                                                        |
| KV / flash attention                      | Q8_0 K/V / on                                          | No smaller-KV experiment yet.                                                                           |
| Output allowance                          | 32,768 including reasoning and final answer            | One Q4 response hit the cap and was retried; 32,768 spent tokens omitted from accepted-response totals. |
| Input limit / compaction reserve          | 98,304 / 32,768                                        | Tokenizer-estimated history triggers around 65,536; two Q4 handoffs succeeded.                          |
| Speculation / MTP                         | None                                                   | Ordinary decoding.                                                                                      |
| Model loading                             | mmap, lazy embeddings, partial CPU placement           | SSD/page-cache behavior matters; layer count does not establish complete GPU residency.                 |

The sampling differences are concrete, but this run cannot establish whether they
helped or hurt quality. The output cap is a local memory/time trade-off, not the
model card's suggested allowance for a 1M-context deployment.

## Reasoning-history evidence

Installed Harbor 0.23.0:

- `agents/terminus_2/terminus_2.py`: `Terminus2Options.interleaved_thinking=False`;
  passes the option into `Chat`.
- `llms/chat.py`: appends role/content; adds `llm_response.reasoning_content` only
  when interleaved thinking is enabled.
- Q4 `result.json.agent_result.metadata.all_messages`: 73 assistant messages,
  zero reasoning-content fields and zero embedded think tags. This is the final
  active history after compaction, not the entire rollout.
- Main/subagent trajectories save reasoning separately. Saving it in an artifact
  does not send it to the next request.
- Served Jinja reads `message.reasoning_content` and defaults `preserve_thinking`
  to true. The template cannot restore missing content.

Source SHA256 for `llms/chat.py`:
`cb986e957a8d0239d7a1d0b7d700efbba249684d56bda9f908d5cb3d6f580ed1`.
Served template SHA256:
`12827f24b742ea4e80cdc12dbcf9622227056b9f797252a3149263d4f9aaadce`.
Other source hashes are in each run's `harbor-source-sha256.txt`.
Artifacts: `/tmp/wyrm2-qwen38-q4-20260926/` and `/tmp/wyrm2-qwen38-iq4-20260926/`.

Visible analysis/plan and tool observations remain in history. Enabling internal
reasoning history could improve continuity or reduce repeated analysis, but also
fills context faster and changes compaction frequency. Test it separately.

## Next settings experiments

1. Finish the current IQ4/Q4 comparison unchanged.
2. On the chosen quant, compare current sampling with explicitly supplied
   `temperature=1.0`, `top_p=0.95`, `top_k=20`, `min_p=0.0`, neutral penalties.
   Record a new configuration rather than relabeling historical results.
3. Separately test Harbor `interleaved_thinking=true` and template
   `preserve_thinking=true`. Inspect the outgoing next-turn request and prompt
   count before a long task. Include summary/Q&A paths; flags alone are not proof.
4. Keep the 32K cap initially. If truncation recurs, test a larger output allowance
   with corresponding input/compaction reserves rather than silently removing it.
5. Compare medium reasoning after these checks. Faster responses need not mean
   faster correct task completion.

No change above is a demonstrated improvement. This audit made no model requests;
the active server and copied recipe remain unchanged.
