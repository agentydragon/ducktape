"""Skill staging for skill-eval rollouts.

Eval rollouts pick a generated ``SPEC: SkillSpec`` and call
`stage_skill` to extract the packaged skill tar into a host directory
ready to bind into the agent's scratch container (see
`eval_sandbox.SKILL_PATH`). Extraction lives in this module — there is
no per-skill staging code.
"""

import tarfile
from dataclasses import dataclass
from pathlib import Path

from skills.skill_spec import SkillSpec
from util.bazel.runfiles import get_required_path


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
