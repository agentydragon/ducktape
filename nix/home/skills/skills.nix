# Shared skill deployment for AI agents (Claude Code, Gemini CLI, etc.)
#
# Returns home.file entries that deploy skills to ~/.{agent}/skills/.
# Each agent module calls this with its target prefix.
#
# Usage:
#   import ./skills.nix { inherit lib siderolabs-docs repoRoot; prefix = ".claude"; }
{
  lib,
  siderolabs-docs,
  prefix,
  repoRoot,
}:
let
  skillsDir = ./.;

  # Skills still in nix/home/skills/ — auto-discovered, deployed recursively
  localSkills = lib.mapAttrs' (
    skillName: _:
    lib.nameValuePair "${prefix}/skills/${skillName}" {
      source = skillsDir + "/${skillName}";
      recursive = true;
    }
  ) (lib.filterAttrs (name: type: type == "directory") (builtins.readDir skillsDir));

  # Skills in repo-root skills/ — explicit file lists (only listed files are deployed)
  repoSkillSpecs = {
    info_gathering = {
      files = [ "SKILL.md" ];
    };
    proxmox_vm = {
      files = [
        "SKILL.md"
        "vm_interact.py"
      ];
    };
    hetzner_vnc_screenshot = {
      files = [
        "SKILL.md"
        "vnc_screenshot.py"
      ];
    };
  };

  repoSkills = lib.concatMapAttrs (
    skillName: spec:
    lib.listToAttrs (
      map (
        fileName:
        lib.nameValuePair "${prefix}/skills/${skillName}/${fileName}" {
          source = repoRoot + "/skills/${skillName}/${fileName}";
        }
      ) spec.files
    )
  ) repoSkillSpecs;

  # External skills fetched from upstream repos
  externalSkills = {
    "${prefix}/skills/siderolabs/SKILL.md".source = "${siderolabs-docs}/public/skill.md";
  };
in
localSkills // repoSkills // externalSkills
