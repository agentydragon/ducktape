# Upstream issue draft

For filing against `UKGovernmentBEIS/inspect_ai`. Not yet posted. Save
as a starting point; trim/edit before sending.

---

## `bash_session` tool silently corrupts JSON-RPC responses larger than `MAX_EXEC_OUTPUT_SIZE`

### Summary

When a `bash_session` tool call returns a JSON-RPC response larger than
`MAX_EXEC_OUTPUT_SIZE` (default 10 MiB), `CircularByteBuffer` truncates
**from the front of stdout**, corrupting the JSON envelope rather than
the `result` payload. The next `JSONRPCResponse.model_validate_json`
call then fails with `Invalid JSON: expected value at line 1 column 1`,
and the entire eval is aborted with no recovery.

The model itself isn't doing anything pathological — a single
reasonable bash command (`grep -R … ..` from the SWE-bench `/testbed`
working dir) walks the container's `/sys`, hits kernel-defined sysfs
symlink cycles, and stderr accumulates faster than `head` can drain
stdout. After ~7 tool calls of accumulated terminal state, the
JSON-RPC response exceeds 10 MiB and the next read corrupts the wire.

### Reproduction

- inspect_ai 0.3.214, inspect_evals 0.10.0
- Task: `inspect_evals/swe_bench` (Verified, sample
  `astropy__astropy-12907`)
- Model: any modern instruct model. Hit on `gpt-oss:20b` via Ollama
  OpenAI-compat, but the bug is wire-protocol-level — model choice
  doesn't matter.
- The model ran `grep -R "def separability_matrix" -n .. | head` from
  `/testbed`, escaping into the container's `/sys`.

**Stand-alone byte-level repro** of how easily the cap is exceeded
(no model required):

```bash
$ docker run --rm -d --name m \
    ghcr.io/epoch-research/swe-bench.eval.x86_64.astropy__astropy-12907:latest \
    sleep 600
$ docker exec m bash -c 'cd /testbed && \
    grep -R "def separability_matrix" -n .. 2>/tmp/err | head -c 0
    wc -c /tmp/err'
18838272 /tmp/err
```

A single command produces **~18 MiB of stderr in ~60 seconds** — already
~1.9× the 10 MiB default cap. Anything that pulls that into a
`bash_session` response will hit this bug.

Resulting traceback:

```text
File "inspect_ai/_util/_json_rpc.py", line 291, in parse_json_rpc_response
    match JSONRPCResponse.model_validate_json(response_str).root:
ValidationError: 1 validation error for JSONRPCResponse
  Invalid JSON: expected value at line 1 column 1
  [type=json_invalid, input_value='rmal/cooling_device13/de... loop\\n", "id": 673}\n', …]
```

### Exact corruption path

1. `bash_session` tool wrapper at `tool/_tools/_bash_session.py:230`
   issues an RPC call.
2. Transport `util/_sandbox/_json_rpc_transport.py:SandboxJSONRPCTransport.__call__`
   runs `sandbox.exec([SANDBOX_CLI, "exec"], input=…)` inside the
   container. The container-side support binary computes the response
   (full terminal state for `bash_session`) and writes it to stdout.
3. `util/_sandbox/exec_remote.py:606` allocates
   `stdout_buffer = CircularByteBuffer(MAX_EXEC_OUTPUT_SIZE)` to
   capture stdout. Default `MAX_EXEC_OUTPUT_SIZE = 10 * 1024**2`
   (`util/_sandbox/limits.py:5`).
4. `util/_subprocess.py:301–316` `CircularByteBuffer.write` discards
   chunks from the front of the buffer when total bytes exceed
   `max_bytes`:

   ```python
   while self._total_bytes > self._max_bytes and len(self._chunks) > 1:
       removed = self._chunks.popleft()
       self._total_bytes -= len(removed)
   if self._total_bytes > self._max_bytes and self._chunks:
       excess = self._total_bytes - self._max_bytes
       self._chunks[0] = self._chunks[0][excess:]
   ```

5. `getvalue()` returns the **last 10 MiB of stdout** — chopping off
   the `{"jsonrpc":"2.0","result":"…` opener of a >10 MiB JSON-RPC
   response.
6. Transport returns the corrupted string to
   `_util/_json_rpc.py:_exec_request`, which hands it to
   `parse_json_rpc_response`.
7. `JSONRPCResponse.model_validate_json` fails at column 1 because the
   string now starts mid-`result`-string.
8. `ValidationError` propagates up, sample errors, eval aborts with
   `Task interrupted (no samples completed before interruption)`.

### Why this is wrong

`CircularByteBuffer` is a reasonable mechanism for capturing free-form
bash output where keeping the latest content is OK. Applying it to a
**structured wire-protocol payload** is not. JSON-RPC envelopes have
prefix-required structure (`{"jsonrpc":"2.0","result":…`); silently
discarding the prefix produces parse failures with no diagnostic
indicating that the truncation happened.

Worse: there's already an application-layer truncation in `bash_session`
that caps the _displayed_ result at ~16 KB (we observed multiple trial
responses arriving at exactly 16 523 chars). But that truncation runs
**after** the JSON is parsed, in the client — so it doesn't help when
the wire response is already corrupted before reaching the parser. The
truncation that would prevent the bug exists; it's just at the wrong
layer.

### Proposed fixes

In rough order of preference:

1. **Make the tool aware of its maximal output size and truncate at
   the application layer.** The container-side support binary that
   serves `bash_session` knows it's serializing a `result` string;
   bound that string to e.g. `MAX_EXEC_OUTPUT_SIZE - JSON_OVERHEAD`
   before writing. The wire envelope then stays small and parses
   cleanly. Other JSON-RPC methods that return large blobs get the
   same treatment. This is the cheapest fix and the most correct: only
   the layer that knows the structure can truncate safely.

2. **Stream stdout in chunks and stop reading once enough bytes have
   arrived to parse a complete envelope** (or detect a truncation
   condition and surface it as a transport-level error). Inspect
   already has a `truncated_output` error pattern in
   `util/_sandbox/limits.py:108` for read-file overflow; reuse it in
   `exec_remote.py` so buffer overflow becomes a first-class error
   rather than silent corruption.

3. **At minimum, raise on `CircularByteBuffer` overflow when capturing
   sandbox-CLI stdout for JSON-RPC.** A `BufferTruncatedError` would
   give the caller a chance to retry with `interrupt` / `restart` or
   abandon the sample, and would surface the actual problem rather
   than a confusing pydantic validation error at column 1.

### Tangential question: does the in-container support binary need to exist?

Inspect runs every JSON-RPC call by `docker exec`-ing a
`SANDBOX_CLI exec` binary inside the container and piping JSON over
stdio. The support binary itself talks to a long-running daemon (also
inside the container) for stateful tools like `bash_session` —
two tiers of in-container process per call.

The host already has `docker exec` (or k8s pod exec, ssh, …); for
`bash_session` a host-side `tmux send-keys` / `tmux capture-pane`
loop through `docker exec` would maintain shell state without any
in-container support binary at all. For stateless tools like
`text_editor`, plain `docker exec` of `cat`/`tee`/`sed` would do.

I assume the in-container support binary exists to abstract over
multiple sandbox backends (docker, k8s, ssh, …) so each backend
doesn't need to know about each tool. That makes some sense — but it
puts the truncation problem in the wrong place. With host-side tools,
the host already controls how much stdout it reads and could bound
responses cleanly without needing a fragile JSON-over-stdio bridge.

Not asking for a refactor; flagging because if the architecture
already has the option of doing the truncation host-side, fix #2
above gets cheaper.

### Workaround

`INSPECT_SANDBOX_MAX_EXEC_OUTPUT_SIZE` env var raises the cap (parsed
in `util/_sandbox/limits.py:87`). Setting it to e.g. 1 GiB delays the
failure but doesn't fix it — long enough agent runs with one
runaway-stderr command will still saturate eventually, and the same
silent-corruption-on-overflow mode persists.
