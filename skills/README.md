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

## Adding a skill

1. Create `skills/<name>/SKILL.md` with YAML frontmatter (`name`, `description`).
   **Do not add `allowed-tools`** unless explicitly approved — it auto-grants
   tool permissions without prompting. See `AGENTS.md`.
   Keep `description` at or below 1024 characters; `//skills:test_frontmatter`
   enforces the current Codex frontmatter limit.
2. Create `skills/<name>/BUILD.bazel` using `skill_package(name, srcs)`
3. Add `//skills/<name>:<name>_files` to the `all_skills` srcs in `skills/BUILD.bazel`
4. After CI builds a new release, update the `skills-tar` flake input and run `home-manager switch`

Skills tied to one component may live next to it instead of under `skills/`
(e.g. `cpap/skill/`, `cluster/skills/`, the debundle skills) — same
`skill_package` macro and `all_skills` registration.
