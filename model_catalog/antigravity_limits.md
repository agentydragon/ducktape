# Antigravity limits: source and bounded live check

Supporting evidence for the [model-roster design](design.md), using its
[shared token vocabulary](design.md#token-limit-vocabulary). Observations are dated
and version-scoped; they do not authorize configuration or client-budget changes.

The values in our roster have provenance, not just client guesses. At CLIProxyAPI
pin `7fac6b15bcfe5ea55c18c9eaec8e5b7e6457d974`, the
[fetcher](https://github.com/router-for-me/CLIProxyAPI/blob/7fac6b15bcfe5ea55c18c9eaec8e5b7e6457d974/cmd/fetch_antigravity_models/main.go#L293-L298)
reads Google's `fetchAvailableModels` response and maps `maxTokens` to
`ContextLength`, and `maxOutputTokens` to `MaxCompletionTokens`. The
[shipped snapshot](https://github.com/router-for-me/CLIProxyAPI/blob/7fac6b15bcfe5ea55c18c9eaec8e5b7e6457d974/internal/registry/models/models.json)
records **1048576 / 65535** for Antigravity `gemini-3.1-flash-lite`,
**200000 / 64000** for its Claude Opus/Sonnet, and **114000 / 32768** for GPT-OSS
120B Medium. This is the historical pinned snapshot; the fresh authenticated
response below differs. The fetcher's renaming alone does not define input versus
combined capacity.

Existing online material helps interpret these fields:

- Google's [Model API reference](https://ai.google.dev/api/models#Model) defines
  `inputTokenLimit` as maximum input tokens and `outputTokenLimit` as maximum
  output tokens. This documents Gemini's vocabulary, not the private Antigravity
  field name by itself.
- [claude-proxy's Antigravity mapping](https://github.com/aeroxy/claude-proxy/blob/09deb1de3c14e1f9e67b2ec2ba91956a1e098e83/src/gemini/models.rs#L300-L335)
  maps `maxTokens` to `inputTokenLimit` and `maxOutputTokens` to `outputTokenLimit`.
  [Jaato's merged #1447](https://github.com/Jaato-framework-and-examples/jaato/pull/1447)
  explicitly treats Gemini output as a separate budget, while reserving output
  within the window for Antigravity Claude. These are third-party interpretations;
  the live check below corroborates the Gemini distinction on our route.
- [Upstream catalogue PR #59](https://github.com/router-for-me/models/pull/59)
  reports fresh production/sandbox `fetchAvailableModels` values of **250000** for
  Claude Opus/Sonnet and **131072** for GPT-OSS, replacing 200000/114000, with output
  fields unchanged. It was open when checked; our own authenticated fetch below
  now corroborates those reported values.

## Fresh Google metadata, 2026-10-05 12:01 UTC

Through AIQuota's existing management integration, CLIProxyAPI queried
`/v1internal:fetchAvailableModels` on `cloudcode-pa.googleapis.com`,
`daily-cloudcode-pa.googleapis.com` and `daily-cloudcode-pa.sandbox.googleapis.com`.
All three returned HTTP 200 and identical model IDs/display names/token fields for
**33 models**. OAuth substitution/refresh stayed inside CLIProxyAPI; only the
selected metadata fields were returned, not credentials or auth-file contents.

| Model / group in the response                  | `maxTokens` | `maxOutputTokens` |
| ---------------------------------------------- | ----------: | ----------------: |
| Claude Opus 4.6 Thinking / Sonnet 4.6          |      250000 |             64000 |
| GPT-OSS 120B Medium                            |      131072 |             32768 |
| Gemini 3.1 / 3.5 Flash Lite                    |     1048576 |             65535 |
| Gemini Pro agent / 3.1 Pro High / Low          |     1048576 |             65535 |
| Gemini 3 Flash; 3.6 / 3.7 / 3.8 Flash variants |     1048576 |             65536 |
| Gemini 3.1 Flash Image                         |      absent |            absent |

Our snapshot's 200000 and 114000 are therefore stale relative to our account's
current response; 3.5 Flash Lite now has a declared pair too. The 65535/65536
output-field distinction is present in Google's response, not just our handwritten
configuration. These are **raw provider-reported fields**, not a claim that every
family's `maxTokens` means maximum input or that both maxima are jointly attainable.
The Flash Lite probe below supplies separate evidence about Gemini semantics.
Do not change consumer budgets, authorize new models, or expose internal entries
merely because they appear in this response.

## Bounded serving-path check

On **2026-10-05**, the cheap-experiments key admitted Antigravity's 3.1/3.5 Flash
Lite routes, not its Claude or GPT-OSS routes. Bounded live requests through
`/v1/chat/completions` used only
`antigravity/ant-messages/gemini-3.1-flash-lite`:

| Observation                                           | Result                                                                                              |
| ----------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Small output-cap control, request `max_tokens=1`      | 13 output tokens, normal stop                                                                       |
| 10000 filler units, five checkpoints                  | 10105 input / 36 output; all five recovered                                                         |
| 1020000 filler units, five checkpoints                | 1020106 input / 38 output; all five recovered                                                       |
| 1048000 filler units, checkpoints plus integers 1–200 | **1048133 input / 727 output / 1048860 total**; all checkpoints and integers recovered, normal stop |
| 1048450 and 1049000 filler units                      | Both HTTP 400: **“The input token count exceeds the maximum number of tokens allowed 1048576.”**    |

Each filler unit was ` a`. Five independently random eight-hex-digit checkpoints
were placed before, between and after four near-equal filler blocks; the final
instruction requested them in order. The last successful request additionally
asked for integers 1–200 to cross the candidate _combined_ boundary with bounded
requested output, not a 64K-output stress test. Usage reported no cached input.
Rejected requests supplied no actual input counts: do not derive an exact
client-visible token ceiling from filler counts or assume no framing overhead.

**For this route, the evidence supports `maxTokens` as an input allowance, not an
input-plus-output ceiling:** the successful total exceeds 1048576, and the error
explicitly calls the limit an input-token limit. Checkpoint recovery is evidence
against simple prefix/tail truncation, not proof of arbitrary-task fidelity over
all tokens. The 65535 output value remains an upstream declaration, not a measured
output maximum; these results do not establish Claude/GPT-OSS semantics or a
universal combined capacity. Preserve the distinction between sourced metadata,
client policy and measured behavior.

The output-cap control agrees with the pinned
[executor](https://github.com/router-for-me/CLIProxyAPI/blob/7fac6b15bcfe5ea55c18c9eaec8e5b7e6457d974/internal/runtime/executor/antigravity_executor_request.go#L61-L96):
it removes `maxOutputTokens` for non-Claude Antigravity requests. Upstream
[PR #4598](https://github.com/router-for-me/CLIProxyAPI/pull/4598) proposes preserving
it (open when checked). This is separate from interpreting the model metadata;
request caps are not a hard cost bound on our current Gemini path.

The investigation stopped after five successful generations (**2078373 input /
816 output tokens total**) and two rejections, plus one tiny count-token call.
The key's published zero prices do not establish free upstream usage; shared
subscription quota and any billing of rejected requests were not measured.
No configuration, authorization, client budget or paused integration was changed.
