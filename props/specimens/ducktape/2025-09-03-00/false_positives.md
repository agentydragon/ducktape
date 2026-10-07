## git_commit_ai/cli.py

- L631 — Re-raises appropriately; acceptable pattern, not a scoped try/except violation. Rationale: exception is
  propagated; no swallowing occurs.

- cli.py:864 — "Incompatible assignment: ai_client assigned ClaudeAI then CodexAI" Rationale: Variable is intentionally
  unannotated; assigning different concrete classes and then using the common surface is valid. This is not a violation
  of our properties; it reflects dynamic union use without upfront annotation. Some type checkers infer the first
  assignment and complain on the second; that is a tooling limitation here, not a code error, since we do not claim a
  narrower declared type. Recommendation: Optionally annotate a union (ai_client: ClaudeAI | CodexAI) or extract a
  minimal Protocol/ABC capturing the methods used at the call sites to satisfy stricter checkers without changing
  behavior.
- cli.py:717 — "ioctl third-arg must be bytes-like; passing str is invalid" Rationale: False positive at runtime on
  CPython 3.13. Python 3.13 docs and CPython source both allow str as the 3rd arg: str is encoded to bytes (UTF‑8) and
  passed on the read-only buffer path. Typeshed’s ioctl stubs often omit str, so some type checkers complain despite
  runtime support. Evidence (authoritative):
  - Python 3.13 docs (fcntl): “The parameter arg can be an integer, a bytes-like object, or a string.” and “A string
    value is encoded to binary using the UTF‑8 encoding.” https://docs.python.org/3.13/library/fcntl.html
  - CPython 3.13 source (Modules/fcntlmodule.c): ioctl accepts writable buffers (w*) or read-only buffers (s*, includes
    str/bytes); read-only path copies up to 1024 bytes and returns bytes.
    https://github.com/python/cpython/blob/v3.13.0/Modules/fcntlmodule.c
  - Typeshed stubs (fcntl.pyi): fcntl.fcntl allows str | ReadOnlyBuffer; ioctl overloads typically list
    Buffer/WriteableBuffer but not str, causing type-checker flags.
    https://github.com/python/typeshed/blob/07557a4316d246b4315f600fd4c9734297d6bc92/stdlib/fcntl.pyi Guidance: Use
    bytearray/array for ioctls that write into the caller’s buffer (mutable) or to avoid the 1024-byte limit; str/bytes
    are fine for read-only calls that return a bytes result.
- cli.py:51 — Missing declared dependency for GitPython (deptry) Rationale: False positive. The package declares
  `GitPython>=3.1` in llm/git-commit-ai/pyproject.toml under `[project.dependencies]`. Evidence:
  /code/llm/git-commit-ai/pyproject.toml lines 10–13 include `GitPython>=3.1`.
