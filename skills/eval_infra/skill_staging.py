"""Skill staging for skill-eval rollouts.

A `SkillSpec` names a packaged skill: the runfiles location of its
`pkg_tar` and the directory the tar prefixes its files with. Each skill
that wants to be eval-mountable ships a small `skill_spec.py` next to its
SKILL.md that exports a `SPEC: SkillSpec` constant; eval rollouts pick a
SPEC and call `stage_skill` to extract the tar into a host directory ready
to bind into the agent's scratch container.

Extraction lives in this module — there is no per-skill staging code.
"""

import tarfile
from dataclasses import dataclass
from pathlib import Path

from util.bazel.runfiles import get_required_path


@dataclass(frozen=True)
class SkillSpec:
    """Bazel runfiles + package layout for one packaged skill.

    Attributes:
        tar_rlocation: Runfiles path of the skill's `<name>_tar` tar
            artifact (e.g. ``"_main/skills/info_gathering/info_gathering_tar.tar"``).
        package_name: The directory the tar prefixes its files with
            (e.g. ``"info_gathering"``). Both the `pkg_tar`'s
            ``package_dir`` and the inner SKILL.md's parent dir.
    """

    tar_rlocation: str
    package_name: str


@dataclass(frozen=True)
class StagedSkill:
    """A skill extracted to a host directory ready to bind into a container.

    Attributes:
        md_text: SKILL.md text, for inlining into the system prompt.
        files_path: Host directory containing the skill's files
            (SKILL.md plus any examples). Bind-mount this read-only into
            the scratch container.
    """

    md_text: str
    files_path: Path


def stage_skill(spec: SkillSpec, dest_dir: Path) -> StagedSkill:
    """Extract `spec`'s tar into `dest_dir` and return the staged skill."""
    tar_path = get_required_path(spec.tar_rlocation)
    with tarfile.open(tar_path) as tf:
        tf.extractall(dest_dir, filter="data")
    files_path = dest_dir / spec.package_name
    md_text = (files_path / "SKILL.md").read_text()
    return StagedSkill(md_text=md_text, files_path=files_path)
