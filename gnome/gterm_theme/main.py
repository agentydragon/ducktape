"""GNOME Terminal theme follower — copies a source profile's colors to a mutable "Auto" profile."""

import subprocess
from uuid import UUID

from absl import app, flags, logging
from gi.repository import Gio

_PROFILE = flags.DEFINE_string("profile", None, "UUID of the source profile")
_TARGET_PROFILE = flags.DEFINE_string("target_profile", None, "UUID of the target profile")


class GSettingsProfiles:
    def __init__(self):
        self.settings = Gio.Settings.new("org.gnome.Terminal.ProfilesList")

    @property
    def profile_uuids(self) -> set[UUID]:
        """Get all profile UUIDs from gsettings."""
        return {UUID(x) for x in self.settings.get_strv("list")}


def copy_profile_colors(source_uuid: UUID, target_uuid: UUID) -> None:
    """Copy color-related properties from one profile to another."""
    profiles_path = "/org/gnome/terminal/legacy/profiles:/"
    for prop in [
        "background-color",
        "foreground-color",
        "palette",
        "bold-color",
        "bold-color-same-as-fg",
        "cursor-colors-set",
        "cursor-background-color",
        "cursor-foreground-color",
        "highlight-background-color",
        "highlight-foreground-color",
        "font",
        "use-theme-colors",
    ]:
        # dconf read and write use the same GVariant text format.
        value = subprocess.check_output(["dconf", "read", f"{profiles_path}:{source_uuid}/{prop}"], encoding="utf-8")
        value = value.rstrip("\n")
        if not value:
            logging.warning("Property %s not found in source profile %s", prop, source_uuid)
            continue
        logging.info(f"Copy {prop}={value}")
        subprocess.run(["dconf", "write", f"{profiles_path}:{target_uuid}/{prop}", value], check=True)


def _main(_):
    profiles = GSettingsProfiles()
    source_uuid = UUID(_PROFILE.value)
    target_uuid = UUID(_TARGET_PROFILE.value)
    profile_uuids = profiles.profile_uuids
    for role, profile_uuid in (("Source", source_uuid), ("Target", target_uuid)):
        if profile_uuid not in profile_uuids:
            raise ValueError(f"{role} profile {profile_uuid} is not in the GNOME Terminal profile list")
    copy_profile_colors(source_uuid, target_uuid)


def main():
    # Entrypoint used by the console_script. Enforce required flag and run.
    flags.mark_flag_as_required(_PROFILE.name)
    flags.mark_flag_as_required(_TARGET_PROFILE.name)
    app.run(_main)
