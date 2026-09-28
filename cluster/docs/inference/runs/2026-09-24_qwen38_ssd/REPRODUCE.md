# Reproduce the Qwen3.8 SSD probes

Run these targets from the repository Nix devshell on the host running llama.cpp.
The downloader reads `HF_TOKEN` from the environment. It passes the authorization
header to curl through stdin, never through argv or a file. It resumes `.partial`
files, verifies the manifest size and SHA-256 before publishing each checkpoint, caps
curl at 60 MiB/s with three retries, and requires 200 GiB of free space beyond the
remaining download. Already verified checkpoint files are skipped.

```bash
nice -n 19 ionice -c 2 -n 7 \
  bazelisk run //cluster/docs/inference/runs/2026-09-24_qwen38_ssd:download_checkpoints -- \
  --output-dir /var/lib/llm-models-ssd
```

The probe driver sends the captured coding request unchanged unless `--model` is
specified. The long-context probe reads each source blob from commit
`5883d2dc7297bd234ca640067e91136da2f45815`, verifies its manifest SHA-256, then
uses the server's `/tokenize` and `/detokenize` endpoints to form the first 24,000
source token IDs, then checks the serialized request against the recorded SHA-256.
The tool probe makes two chat-completion requests and supplies a fixed synthetic
`read_file` result between them; it does not execute a tool.

Each selector writes the exact JSON request bodies, raw JSON responses, and a timing
file with HTTP status, server `timings`, and usage under a selector subdirectory.
Long-context tokenization and detokenization requests are captured as well. Keep the
output directory outside the Git checkout because the long-context request includes
the source payload. Authorization headers and API keys are never saved.

```bash
bazelisk run //cluster/docs/inference/runs/2026-09-24_qwen38_ssd:reproduce_probes -- \
  --base-url http://127.0.0.1:18080 \
  --selector short \
  --output-dir /var/lib/llm-models-ssd/probe-captures
```

Use `--selector long` or `--selector tools` to capture those probes in sibling
subdirectories. `--base-url` may include a trailing `/v1`; `--model` overrides the
captured model ID, and `--api-key-file` supplies an optional one-line Bearer key.
Choose a fresh output directory for each run because existing captures are preserved.

Launch the server with the adjacent `dense_gpu1.sh`, `dense_dual.sh`,
`dense_tensor.sh` or `flash_dual.sh` recipe before probing. The tensor recipe needs
its pinned `Dockerfile.nccl227` image; see the run README. Use `--model qwen3.8-flash-next-q4` for Flash; keep the original dense model for an exact request
hash comparison. `CONTEXT_SIZE=32768` selects the long-input launch size.
The pinned source commit must exist locally (`git fetch origin 5883d2dc7297bd234ca640067e91136da2f45815` if needed).

Validation on September 26: source-file hashes and the saved request hash were
checked, and formatting/static checks ran. The new drivers have not been replayed
end to end: a Bazel `--help` attempt failed fetching its Python runtime because the
separate Bazel cache filesystem was full. No model download or inference was
started during that check. CI build status is separate from live replay.
