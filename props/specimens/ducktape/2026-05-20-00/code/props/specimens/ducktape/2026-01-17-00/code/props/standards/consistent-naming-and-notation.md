---
title: Consistent naming and notation
kind: outcome
---

Adopt one clear naming/notation convention per project (or per package) and apply it uniformly. Avoid mixing file/identifier patterns that describe the same concept with different names or layouts.

## Acceptance criteria (checklist)

- Test files follow a single, consistent convention across the project (or per top-level package), e.g., `pkg/test_foo.py` (preferred) or `pkg/foo_test.py`; do not mix patterns within the same scope.
- Choose one test location strategy and stick to it for a given project/package:
  - Co-located: `src/<pkg>/tests/test_*.py`
  - Central: `tests/<pkg>/test_*.py`
    Mixing both within the same project/package is a violation.
- Directory placement is consistent: keep related tests together under their package/module (e.g., `my_service/test_*.py`, `other_service/test_*.py`), not scattered across differently named paths.
- File names use one tokenization scheme consistently (e.g., underscores, no intermix of custom affixes/orderings like `test_pkg_run_bar.py` vs `test_pkg_baz.py`).
- Avoid parallel synonyms for the same concept in names (e.g., `interface` vs `protocol` vs `facade`) unless distinctions are intentional and documented; prefer one obvious name. See also: [Renames must pay rent](./no-random-renames.md).
- Exceptions (legacy pockets, third‑party layout) must be isolated and documented in a short comment or contributing guide; new files conform to the chosen convention.

## Positive examples

Consistent per‑package test layout and naming (central tests/ style):

```text
my_service/
  test_foo.py
  test_bar.py
  test_baz.py
other_service/
  test_xyzzy.py
```

Or, suffix style (keep it consistent):

```text
my_service/
  foo_test.py
  bar_test.py
```

Or, co‑located tests (stick to it project‑wide):

```text
src/
  foo/
    bar.py
    tests/
      test_bar.py
```

## Negative examples

Mixed/misaligned test file naming and placement:

```text
test_my_service_foomethod.py
test_my_service_run_bar.py
my_service/test_baz.py
other_service/test_xyxxy_endpoint.py
```

Mixed tokenization and ordering for the same scope:

```text
my_service/test_run_bar.py
my_service/test_baz.py
my_service/run_baz_test.py   # different pattern
```

Mixing co‑located and central test conventions in one project/package (do not mix):

```text
src/foo/bar.py
src/foo/tests/test_bar.py
tests/foo/test_baz.py   # ❌ mixed conventions
```

## Notes

- Pick one convention per project; consider documenting it in CONTRIBUTING.md; use linters/review to keep it consistent.
- Consistency reduces cognitive load and speeds navigation/grep.
- Related properties: [Renames must pay rent](./no-random-renames.md), [Self‑describing names](./self-describing-names.md).
