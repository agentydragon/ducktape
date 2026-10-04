# TODO

## Tests

- [ ] `test_long_context.py` and `test_responses.py` never run in CI: they are manual
      probes of a live server (vLLM at `http://0.0.0.0:8000`, Ollama at
      `http://localhost:11434`), not hermetic tests, and the tree is
      `# gazelle:exclude`d in the root `BUILD.bazel`. Decide whether to keep them as
      manual scripts (drop the `test_` prefix so they stop looking like tests) or
      turn them into tests that run against a fake server.
