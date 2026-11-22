"""GNOME Terminal theme follower — copies a source profile's colors to a mutable "Auto" profile."""

import ast
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


class ProfileDConf:
    def __init__(self, profile_uuid: UUID):
        self.profile_uuid = profile_uuid

    def _path(self, property_name: str) -> str:
        """Construct the dconf path for the given property."""
        return f"/org/gnome/terminal/legacy/profiles:/:{self.profile_uuid}/{property_name}"

    def read_property(self, property_name: str) -> str | bool | list[str]:
        try:
            out = subprocess.check_output(["dconf", "read", self._path(property_name)]).decode("utf-8").strip()
            if not out:
                raise KeyError
            if out == "true":
                return True
            if out == "false":
                return False
            value = ast.literal_eval(out)
            if isinstance(value, str | bool):
                return value
            if isinstance(value, list) and all(isinstance(x, str) for x in value):
                return value
            raise TypeError(f"Unsupported dconf value type: {type(value)}")
        except subprocess.CalledProcessError as e:
            raise KeyError(f"Reading '{property_name}' from {self.profile_uuid} failed") from e

    def write_property(self, property_name: str, value: bool | str | list[str]) -> None:
        """Write a property to the profile."""
        if value is True:
            v = "true"
        elif value is False:
            v = "false"
        elif isinstance(value, str):
            assert "'" not in value, "Value must not contain single quotes"
            v = f"'{value}'"
        elif isinstance(value, list) and all(isinstance(x, str) for x in value):
            v = repr(value)
        else:
            raise TypeError(f"Unsupported value type: {type(value)}. Must be bool, str, or list[str].")
        subprocess.check_call(["dconf", "write", self._path(property_name), v])


def copy_profile_colors(source_uuid: UUID, target_uuid: UUID) -> None:
    """Copy color-related properties from one profile to another."""
    source_dconf, target_dconf = ProfileDConf(source_uuid), ProfileDConf(target_uuid)
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
        try:
            value = source_dconf.read_property(prop)
        except KeyError:
            logging.warning("Property %s not found in source profile %s", prop, source_uuid)
            continue
        logging.info(f"Copy {prop}={value}")
        target_dconf.write_property(prop, value)


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
