# Skills

Agent skills for Claude Code, Gemini CLI, OpenCode, and other agents.

## Deployment

Each skill is its own `.skill` zip built by the `skill_package` macro (`defs.bzl`),
`//skills/<name>:<name>_skill`, released by CI as a `skill-<name>` GitHub release.

- **Local machines**: `flake.nix` fetches each Nix-enabled `skill-*` pin into one
  flat directory of `<name>/` subdirs (`skills-tar`); `nix/home/skills.nix` links it
  into each agent home (`~/.claude/skills/`, `~/.gemini/skills/`,
  `~/.codex/skills/`, …).
- **Claude Code Web**: the `devtools` profile's `ducktapePkgs.skills` assembles the
  same artifacts at `share/claude-hooks/skills/`; `devinfra/claude/web_setup.sh`
  symlinks each into `~/.claude/skills/`, preserving Anthropic's defaults.

### Refreshing managed skills

Skill source, released archives, artifact pins, and installed files are separate
states. To deploy a change, let CI publish the registered skill archives and
`sync-pins` update `nix/artifact-pins.json`, then apply the host's normal
[Nix configuration](../nix/README.md#usage). NixOS hosts with inline
home-manager use their system activation; standalone profiles use
home-manager. Preserve the managed links in agent homes.

Compare the resolved installed directory with the pinned archive, including
bundled references as well as `SKILL.md`: reference-only changes also require
a deployment. Start a fresh agent session after activation so discovery uses
the installed skill descriptions and files.

## Registry

`skills_registry.json` (hand-written) decides what is built and released. Each
entry carries the skill `name`, release `pkg`, Bazel `target`, released `filename`,
and optional `nix` setting (default `true`; `false` means released but not pinned
or deployed by Nix). `release_assets.py` resolves each target's path from the
build event stream. Consumers: `.github/workflows/release.yml`,
`devinfra/ci/artifacts.py` (pin sync), `flake.nix` / `nix/packages/default.nix`. A
`skill_package` with no registry entry is valid — just not released or deployed.

## Adding a skill

1. `skills/<name>/SKILL.md` with frontmatter `name`, `description` (≤1024 chars,
   enforced by `//skills:test_frontmatter`). No `allowed-tools` without approval
   (<AGENTS.md>).
2. `skills/<name>/BUILD.bazel` with `skill_package(name, srcs)`.
3. Add a `skills_registry.json` entry to ship it.
4. After CI releases `skill-<name>`, `sync-pins` seeds its pin in
   `nix/artifact-pins.json` (unless `nix: false`); then activate the host's Nix config. Nix
   assembly skips registered skills without a pin, so the first release can precede
   the pin.

Component-specific skills may live beside their component (`cpap/skill/`,
`cluster/skills/`, the debundle skills) — same macro, same registry.
