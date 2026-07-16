# Skills

Agent skills for Claude Code, Gemini CLI, OpenCode, and other AI agents.

## Deployment

Skills are built by Bazel (`skill_package` macro in `defs.bzl`) — each skill is a
`.skill` zip (`//skills/<name>:<name>_skill`) — and packaged into a combined
`.skill` zip (`//skills:all_skills`). CI publishes this combined archive as a
GitHub release artifact.

**Local machines**: `flake.nix` unpacks the release artifact as the `skills-tar`
flake input. `nix/home/skills.nix` then creates Home Manager `home.file` entries
for each configured agent home (`~/.claude/skills/`, `~/.gemini/skills/`,
`~/.codex/skills/`, etc.).

**Claude Code Web**: the `devtools` profile includes `ducktapePkgs.skills`,
which extracts the same release artifact under the Nix profile at
`share/claude-hooks/skills/`. `devinfra/claude/web_setup.sh` then symlinks each
profile skill directory into `~/.claude/skills/`, preserving Anthropic's
preinstalled default skills.

## Skill registry

`skills_registry.json` is the hand-written source of truth for which skills are
built, released, pinned, and deployed. Each entry names a skill and how it maps
to Bazel/release/pin identifiers:

- `name` — the skill (also the `.skill` subdir and its `~/.claude/skills/` dir)
- `pkg` — its release/tag/pin name, always `skill-<name>`
- `target` / `output` — the `skill_package` archive target and its `bb-out/` path
- `filename` — the release asset, always `<name>.skill`

Consumed by `.github/workflows/release.yml` (release matrix),
`devinfra/ci/artifacts.py` (pin sync), and `flake.nix` / `nix/packages/default.nix`
(Nix assembly). A skill that has a `skill_package` but no registry entry simply
isn't released or deployed — a valid state, not an error.

## Adding a skill

1. Create `skills/<name>/SKILL.md` with YAML frontmatter (`name`, `description`).
   **Do not add `allowed-tools`** unless explicitly approved — it auto-grants
   tool permissions without prompting. See `AGENTS.md`.
   Keep `description` at or below 1024 characters; `//skills:test_frontmatter`
   enforces the current Codex frontmatter limit.
2. Create `skills/<name>/BUILD.bazel` using `skill_package(name, srcs)`
3. To ship it, add an entry to `skills_registry.json` (see the fields above).
4. After CI publishes the `skill-<name>` release, `sync-pins` seeds the pin in
   `nix/artifact-pins.json`; then run `home-manager switch`.

Skills tied to one component may live next to it instead of under `skills/`
(e.g. `cpap/skill/`, `cluster/skills/`, the debundle skills) — same
`skill_package` macro; register them the same way.
