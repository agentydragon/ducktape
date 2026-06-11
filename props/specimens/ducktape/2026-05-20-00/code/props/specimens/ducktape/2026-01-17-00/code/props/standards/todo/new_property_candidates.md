# Conversion candidates from adgn_instructions (prioritized)

Criteria

- Prefer rules that are precise, objective, and easy to check; avoid wiggly/nuanced ones for now.

2. logging-no-exception-duplication

- Rule: Don’t embed exception text into logger.error/exception messages; rely on exc_info.
- Source: general/code/python.md

3. tests-pytest-layout

- Rule: Tests live in `test_*.py` files co-located with the code; no `__main__` test harnesses.
- Source: general/code/python.md

4. hamcrest-single-item

- Rule: For single-element matching use has_item, not has_items.
- Source: general/code/python.md

5. avoid-broad-except

- Rule: Do not catch broad Exception; catch specific, expected exceptions only.
- Source: general/code/defensive.md

6. typing-self-reference

- Rule: Use typing.Self (or future annotations) for self-referential returns; do not use string class names.
- Source: general/code/python.md

Deprioritized for later (more nuanced)

- aggressive-dry (principle-level)
- document-current-state (judgment-heavy)
- proper-serde-libs (better as decomposed sub-rules per format)

Process, not outcome:

- refactor-tools-libcst-semgrep (process/behavioral)
- rg-over-grep (tooling/process, not code outcome)

Probably skip:

- ascii-art-prefer-generated (harder to detect reliably)
