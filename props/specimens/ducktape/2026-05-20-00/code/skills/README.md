# Skills

Agent skills for Claude Code, Gemini CLI, OpenCode, and other AI agents.

## Deployment

Skills are built by Bazel (`skill_package` macro in `defs.bzl`) and packaged into a combined tarball (`//skills:all_skills_tar`). CI publishes this tarball as a GitHub release artifact (`skills-tar` flake input). Nix home-manager deploys skills from the tarball to `~/.claude/skills/`, `~/.gemini/skills/`, etc. via `nix/home/skills.nix`.

**Local machines**: `skill_package()` BUILD targets → `all_skills_tar` → CI release → Nix flake input (`skills-tar`) → home-manager `home.file` entries (`nix/home/skills.nix`).

**Claude Code Web**: `devinfra/claude/web_setup.sh` extracts the tarball into `~/.claude/skills/`.

## Adding a skill

1. Create `skills/<name>/SKILL.md` with YAML frontmatter (`name`, `description`).
   **Do not add `allowed-tools`** unless explicitly approved — it auto-grants
   tool permissions without prompting. See `AGENTS.md`.
   Keep `description` at or below 1024 characters; `//skills:test_frontmatter`
   enforces the current Codex frontmatter limit.
2. Create `skills/<name>/BUILD.bazel` using `skill_package(name, srcs)`
3. Add `//skills/<name>:<name>_tar` to `all_skills_tar` deps in `skills/BUILD.bazel`
4. After CI builds a new release, update the `skills-tar` flake input and run `home-manager switch`
