# aspect_rules_py awk escape warning

## Symptom

```
awk: cmd. line:8: warning: escape sequence `\.' treated as plain `.'
```

Emitted during `bazel build` for any target using `py_image_layer` (e.g.
`//airlock:layers_manifests`).

## Root cause

`py_image_layer.bzl` in `aspect_rules_py` defines `default_layer_groups` with
regex patterns like:

```python
"interpreter": "\\\\.runfiles/[^/]*?python[^/]*?(x86|arm64|aarch64).*?/",
"packages": "\\\\.runfiles/.*/site-packages",
```

After Starlark string unescaping, this becomes `\\.runfiles/...` in the
generated awk script. The awk script uses these regexes inside double-quoted
strings (`$$1 ~ "\\."`) rather than awk regex literals (`$$1 ~ /\\./`).

In POSIX awk, `\.` inside a double-quoted string is not a recognized escape
sequence. GNU awk treats it as a literal `.` (which is the intended behavior)
but emits a warning.

## Fix

Upstream should either:

- Use `[.]` instead of `\\.` in the regex patterns, or
- Use awk regex literal syntax (`/pattern/`) instead of string syntax (`"pattern"`)

## Status

- Still present on upstream `main` as of 2026-03-07.
- Upstream repo: <https://github.com/aspect-build/rules_py>
- File: `py/private/py_image_layer.bzl`, `default_layer_groups` dict (lines ~48-54).
- Our pinned version: 1.8.4.
- The warning is cosmetic — matching behavior is correct.
