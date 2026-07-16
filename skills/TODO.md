# skills TODO

- **Per-skill deployment routing.** Today every configured agent home
  (`~/.claude`, `~/.gemini`, `~/.codex`, `~/.config/opencode`) gets _all_ skills
  uniformly — `nix/home/skills.nix` deploys every subdir of the assembled skills
  dir to every prefix. Support routing a skill to specific homes (and possibly
  specific hosts). `skills_registry.json` is already routing-ready: add an
  optional per-skill `targets` field (absence = all homes), and have `mkSkills`
  in `nix/home/skills.nix` filter by the calling home. Deferred from the
  per-skill-artifacts migration (kept deploy-everywhere for that change).
