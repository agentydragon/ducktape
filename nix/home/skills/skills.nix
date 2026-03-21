# Shared skill deployment for AI agents (Claude Code, Gemini CLI, OpenCode, etc.)
#
# Returns a function: prefix -> home.file entries that deploy skills to ~/{prefix}/skills/.
# Skills are consumed from a CI-built tarball (skills-tar flake input).
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

  # All skills and the files they deploy. Must match skill_package() srcs in BUILD.bazel.
  repoSkillSpecs = {
    backtrace = {
      files = [ "SKILL.md" ];
    };
    branch_splitter = {
      files = [
        "SKILL.md"
        "validate_dag_split.py"
      ];
    };
    buildbuddy_api = {
      files = [ "SKILL.md" ];
    };
    forensic_surgeon = {
      files = [ "SKILL.md" ];
    };
    hetzner_vnc_screenshot = {
      files = [ "SKILL.md" ];
    };
    info_gathering = {
      files = [ "SKILL.md" ];
    };
    proxmox_vm = {
      files = [ "SKILL.md" ];
    };
    session_logs = {
      files = [
        "SKILL.md"
        "analyze-session.sh"
        "find-current-session.sh"
      ];
    };
    superforecaster = {
      files = [ "SKILL.md" ];
    };
  };
in
# Return a function that generates home.file entries for a given prefix.
prefix:
let
  repoSkills = lib.concatMapAttrs (
    skillName: spec:
    lib.listToAttrs (
      map (
        fileName:
        lib.nameValuePair "${prefix}/skills/${skillName}/${fileName}" {
          source = "${skillsSrc}/${skillName}/${fileName}";
        }
      ) spec.files
    )
  ) repoSkillSpecs;

  externalSkills = {
    "${prefix}/skills/siderolabs/SKILL.md".source = "${siderolabs-docs}/public/skill.md";
  };
in
repoSkills // externalSkills
