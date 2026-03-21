# Shared skill deployment for AI agents (Claude Code, Gemini CLI, OpenCode, etc.)
#
# Returns a function: prefix -> home.file entries that deploy skills to ~/{prefix}/skills/.
# Skills are consumed from a CI-built tarball (skills-tar flake input).
# Skill contents are controlled by skill_package(srcs=...) in each skill's BUILD.bazel.
#
# Usage:
#   let mkSkills = import ../skills/skills.nix { inherit lib pkgs siderolabs-docs skills-tar; };
#   in mkSkills ".claude"
{
  lib,
  pkgs,
  siderolabs-docs,
  skills-tar,
}:
let
  # Unpack the CI-built skills tarball into the Nix store (once, shared across prefixes).
  skillsSrc = pkgs.runCommand "skills-unpacked" { } ''
    mkdir -p $out
    tar xf ${skills-tar} -C $out
  '';

  # Auto-discover skill directories from the unpacked tarball.
  skillDirs = lib.filterAttrs (_: type: type == "directory") (builtins.readDir skillsSrc);
in
# Return a function that generates home.file entries for a given prefix.
prefix:
let
  repoSkills = lib.mapAttrs' (
    skillName: _:
    lib.nameValuePair "${prefix}/skills/${skillName}" {
      source = "${skillsSrc}/${skillName}";
      recursive = true;
    }
  ) skillDirs;

  externalSkills = {
    "${prefix}/skills/siderolabs/SKILL.md".source = "${siderolabs-docs}/public/skill.md";
  };
in
repoSkills // externalSkills
