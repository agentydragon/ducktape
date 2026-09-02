# Live capture probe

`//x/agentplane/capture:live_capture` runs one Claude or Codex scenario against a real model
endpoint and records the whole exchange: the native stdio frames on both sides and the upstream
LiteLLM request/response bodies, passed through a header-blind recording proxy that can drop the
connection at a named SSE packet. It is a discovery tool for provider behavior. The scenarios and
launch flags come from <../native/README.md>; the behavioral contract lives in the scripted tests
in <../harness_tests/README.md>, whose scripts are written and repaired by reading these logs.

```sh
bazel run //x/agentplane/capture:live_capture -- \
  --provider codex --scenario shell --binary /path/to/codex \
  --model cheap-model --endpoint http://litellm.example/v1 \
  --credential-file "$key_file" --workspace "$tmp/workspace" --output "$tmp/capture"
```

Scenarios: `launch`, `baseline`, `shell`, `file_edits`, `steering`, `second_input`, `interrupt`,
`idle_resume`, `connection_retry`, `connection_exhaustion`, `post_failure_follow_up`,
`post_exhaustion_follow_up`. The connection scenarios close the model stream at a named complete SSE
packet (`message_start`/`response.created`, a text delta, or the response headers on later
attempts); a loss happens only after that packet reached the native client, never at an arbitrary
socket boundary.

## Output

```text
metadata.json          # provider, scenario, model
stdin.jsonl            # ordered native frames written to the process
stdout.jsonl           # ordered native frames read from the process
stderr.jsonl           # bounded diagnostics
llm-requests.jsonl     # ordered upstream request bodies
llm-responses.jsonl    # ordered upstream response chunks and loss markers
```

Payloads are stored as UTF-8 wire text. The recording boundary excludes HTTP headers, cookies,
environment variables, OAuth state, credentials, and private user data. The output is investigation
evidence: keep it outside Git. It carries the full system prompt and tool schemas on every request,
volatile ids, and the credential-bearing session, and nothing consumes it mechanically.

The credential is supplied to the native process only for the configured experiment and is neither
recorded nor inspected. No capture path uses a PTY, tmux, terminal scraper, prompt heuristic,
Kubernetes mutation, or `kubectl exec` protocol path.
