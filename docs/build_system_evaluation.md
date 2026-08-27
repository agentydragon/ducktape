# Build System Decision: Bazel over Pants

Pants was evaluated as the unified build system and rejected; Bazel was chosen and
adopted (see <../README.md>). Constraints that killed Pants:

- **No Rust support** — this repo builds Rust alongside Python; Bazel's `rules_rust`
  is mature.
- **Weaker multi-language story** — Python-first, with C/C++, TypeScript, and Nix
  integration weak or absent; Bazel has rulesets for all of them.
- Pants's main draw — built-in cached lint/typecheck — stopped differentiating once
  `aspect_rules_lint`-style Bazel aspects closed that gap (ruff + mypy run here as
  aspects).
