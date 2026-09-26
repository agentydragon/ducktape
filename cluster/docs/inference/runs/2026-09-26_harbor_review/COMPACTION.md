# Compaction: agent choice, not a Terminal-Bench restriction

September 26 follow-up: the earlier focus on Mini-SWE omitted an existing option.
Terminal-Bench's reference agent Terminus-2 supports context summarization, enabled
by default. Both the [upstream documentation](https://github.com/harbor-framework/harbor/blob/main/docs/content/docs/agents/terminus-2.mdx)
and installed Harbor 0.23.0 source confirm `enable_summarize=True` and a proactive
trigger when estimated free context falls below 8,000 tokens. Its summary, questions,
and answers calls are awaited sequentially. It also has context-overflow recovery.
Thus Terminal-Bench can evaluate agents with smaller windows across long trajectories;
AA's no-compaction Mini-SWE protocol is a particular agent configuration.

A million cumulative generated tokens is not the same as a million-token request.
With compaction, a trajectory can exceed its window many times; without compaction,
retained conversation growth eventually prevents another request. Cumulative input
usage also repeatedly counts previous history, and should not be read as peak context.

## Next practical comparison

Keep the same predetermined tasks/verifiers, but use Terminus-2 with summarization
or Harbor's OpenCode adapter, which accepts a provider/model config overlay. OpenCode
has the strongest direct user evidence here: successful long knowledge-management
work with compaction. Terminus-2 offers an established neutral reference agent.
Neither compacting setup has yet been verified against this local endpoint.

If natural compaction does not occur or fails, force a compaction in a targeted test,
retain the summary and before/after request sizes, and verify resumed actions recover
facts needed by the task. Declare context and output limits explicitly; reserve room
for summary output and subsequent prompts. Repeat the boundary once to test continued
operation. Keep all summarization calls serial on the same local model. Report the
agent/configuration with the score, and retain compaction costs and failures.

A known [tokenizer-estimation issue](https://github.com/harbor-framework/harbor/issues/2382)
can affect Terminus-2's custom-model compaction trigger. Compare its estimate with
llama.cpp's actual token counts and allow measured headroom; a default-enabled setting
is not proof that this Qwen endpoint compacts correctly. Inspect overflow recovery
against the actual API error shape as well.

The user subsequently directed the queue to run one actual frozen Terminal-Bench
job at 128K with Terminus-2 and observe natural compaction. The synthetic prerequisite
was dropped. The [queue recipe](../2026-09-26_qwen38_queue/README.md) is authoritative
for current limits and sequence. No compaction event is required for a completed task
to count; absent events simply leave compaction unverified.

## Evidence portfolio

Terminal-Bench is one controlled hard-task signal, not the objective. Combine a small
frozen subset with representative repo changes (functional checks and diff review),
long knowledge-management tasks (fact preservation and completion), and usability
measurements (prefill, decoding, total time, compaction overhead, desktop headroom).
Prefer independently checkable outcomes to self-reported model success. Keep context
and quant comparisons bounded, then give a promising alternative model a feasibility
pass. Do not spend the program optimizing one benchmark at the expense of useful work.
