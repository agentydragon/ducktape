"""The console's non-secret deploy catalog (`haku.console.identity.config.ConsoleConfigFile`).

Secret leaves stay out of it: the Deployment overlays them from Secrets through
`HAKU_CONSOLE__*` environment variables (`console.py` names each one).
"""

from __future__ import annotations

from haku.console.identity.config import AccessProfile, ConsoleConfigFile


def config() -> ConsoleConfigFile:
    return ConsoleConfigFile(
        access_profiles=[AccessProfile(id="manual-review")], default_access_profile_id="manual-review"
    )
