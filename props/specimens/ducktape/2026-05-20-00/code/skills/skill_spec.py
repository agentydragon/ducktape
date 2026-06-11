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
        tar_rlocation: Runfiles path of the skill's `<name>_tar` tar
            artifact, e.g. `_main/skills/info_gathering/info_gathering_tar.tar`.
        package_name: Directory the tar prefixes its files with. This
            matches the `pkg_tar` package_dir and the inner SKILL.md's
            parent directory.
    """

    tar_rlocation: str
    package_name: str
