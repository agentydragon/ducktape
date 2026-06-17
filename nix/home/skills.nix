# Shared skill deployment for AI agents (Claude Code, Codex, Gemini CLI, OpenCode, etc.)
#
# Returns a function: { prefix, mode ? "recursive" } -> home.file entries that deploy skills to
# ~/{prefix}/skills/.
# Skills are consumed from a CI-built `.skill` archive (skills-tar flake input).
# Skill contents are controlled by skill_package(srcs=...) in each skill's BUILD.bazel.
#
# Usage:
#   let mkSkills = import ./skills.nix { inherit lib pkgs siderolabs-docs skills-tar; };
#   in mkSkills { prefix = ".claude"; }
{
  lib,
  pkgs,
  siderolabs-docs,
  skills-tar,
}:
let
  # The CI-built skills archive, already unpacked into the Nix store (once, shared across prefixes).
  skillsSrc = skills-tar;
  mkSiderolabsDir = pkgs.runCommand "siderolabs-skill" { } ''
    mkdir -p "$out"
    cp ${siderolabs-docs}/public/skill.md "$out/SKILL.md"
  '';

  # Auto-discover skill directories from the unpacked archive.
  skillDirs = lib.filterAttrs (_: type: type == "directory") (builtins.readDir skillsSrc);
in
# Return a function that generates home.file entries for a given prefix.
{
  prefix,
  mode ? "recursive",
}:
let
  linkDir = mode == "directory-symlink";
  repoSkills = lib.mapAttrs' (
    skillName: _:
    lib.nameValuePair "${prefix}/skills/${skillName}" {
      source = "${skillsSrc}/${skillName}";
    }
    // lib.optionalAttrs (!linkDir) { recursive = true; }
  ) skillDirs;

  externalSkills =
    if linkDir then
      {
        "${prefix}/skills/siderolabs".source = mkSiderolabsDir;
      }
    else
      {
        "${prefix}/skills/siderolabs/SKILL.md".source = "${siderolabs-docs}/public/skill.md";
      };
in
repoSkills // externalSkills
