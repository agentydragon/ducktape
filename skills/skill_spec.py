"""Public Bazel runfiles identity for packaged skills.

`skill_package` generates `<name>_skill_spec` modules that export a
`SPEC: SkillSpec` constant. Eval infrastructure can consume that spec,
but the spec type itself is not eval-specific.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillSpec:
    """Bazel runfiles + package layout for one packaged skill.

    Attributes:
        archive_rlocation: Runfiles path of the skill's `.skill` zip
            artifact, e.g. `_main/skills/info_gathering/info_gathering.skill`.
        package_name: Directory the archive prefixes its files with. This
            matches the skill_package name and the inner SKILL.md's parent
            directory.
    """

    archive_rlocation: str
    package_name: str
