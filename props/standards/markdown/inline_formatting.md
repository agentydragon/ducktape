---
title: Markdown inline formatting for code identifiers, flags, paths, and URIs
kind: outcome
---

Inline syntactic elements in Markdown are properly formatted: code spans for commands, flags, identifiers, and paths; links or autolinks for HTTP(S) URLs; code spans for non-linkable URIs/URNs.

## Acceptance criteria (checklist)

- Executables and script names use inline code: `some-script.sh`
- Flags/options and invocations use inline code: `--flag=value`, `tool --flag=value`, `foo_method(...)`
- Identifiers use inline code when referenced in prose: `FooClass`, `foo_method`, `CONSTANT_NAME`
- File/system paths use inline code: `src/some/path.py`, `/etc/hosts`
- HTTP/HTTPS URLs are linkified: `<https://example.com/path>` or `[useful link](https://example.com/path)`
- Non-HTTP URIs/URNs use inline code unless a renderer will linkify them: `gs://bucket/key`, `s3://bucket/object`, `az://container/blob`
- Do not leave these tokens as plain text in prose; do not use quotes as a substitute for code spans
- Multiline code snippets use fenced code blocks (`…`) with an appropriate language tag when applicable (e.g., `python`, `bash`, `markdown`). Use inline code only for single-line fragments
- Unordered lists use `-`, `*`, or `+` markers (GFM/CommonMark); do not use Unicode bullets like •; indent nested items consistently

## Positive examples (proper inline code and links)

```markdown
Run `some-script.sh` with `--dry-run=true`.
The `FooClass` exposes `foo_method(...)` in `src/svc/main.py`.
Logs are archived to `gs://my-bucket/logs/2025-08-27/`.
See <https://example.com/docs/tooling> for details.
```

### Positive examples (multiline code blocks)

```python
def add(a: int, b: int) -> int:
    return a + b
```

```bash
grep -R "pattern" src/ | wc -l
```

### Positive examples (lists)

```markdown
- Parent item
  - Nested item

* Alternate marker

- Another valid marker
```

## Negative examples (plaintext tokens, missing links)

```markdown
Run some-script.sh with --dry-run=true.
The FooClass exposes foo_method(...) in src/svc/main.py.
Logs are archived to gs://my-bucket/logs/2025-08-27/.
See https://example.com/docs/tooling for details.
```

### Dunder/underscore pitfalls

Using bare dunders causes emphasis; protect with code spans.

#### Negative examples (one line each)

```text
my favorite variable is __init__ and constant is __ALL__, edit src/some_module/my__file__.py
```

#### Positive examples (one line each)

```markdown
my favorite variable is `__init__` and constant is `__ALL__`, edit `src/some_module/my__file__.py`
```
