"""SkillSpec for the eval-only "empty skill" tar (the off-arm payload)."""

from skills.eval_infra.skill_staging import SkillSpec

SPEC = SkillSpec(tar_rlocation="_main/skills/eval_infra/empty_skill_tar.tar", package_name="empty_skill")
