"""Tunable constants for the credit system (see plans/credit_system_v2.md).

Kept in one module so game balance can be tweaked without touching
business logic. Milestone/break constants land with their phases.
"""

from decimal import Decimal

# Streak
DAILY_STREAK_STUDY_THRESHOLD_SECONDS = 300  # 5 minutes of study qualifies a day
STREAK_BONUS_PER_DAY = Decimal("0.01")  # +1% credit bonus per streak day
STREAK_BONUS_CAP = Decimal("1.0")  # bonus cap: +100% (2x total multiplier)
REST_DAY_STREAK_INTERVAL = 14  # streak days to earn 1 rest day

# Daily bonus
DAILY_FIRST_BONUS = Decimal(30)  # credits, first time over the threshold each day
