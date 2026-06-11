# Extracted GNOME terminal profile switching functionality
# Originally from adgn.gnome.switch_gnome_terminal_profile

import ast
import subprocess
import uuid
from uuid import UUID
from xml.etree import ElementTree

import dbus
from absl import app, flags, logging
from gi.repository import Gio

_PROFILE = flags.DEFINE_string("profile", None, "Name or UUID of profile to set everywhere")

# Source profiles to copy colors from in auto mode
THEME_SOURCE_PROFILES = {"light": "Solarized Light", "dark": "Solarized Dark"}

AUTO_PROFILE_NAME = "Auto"
GSETTINGS_PROFILES_LIST = Gio.Settings.new("org.gnome.Terminal.ProfilesList")


class GSettingsProfiles:
    def __init__(self):
        self.settings = Gio.Settings.new("org.gnome.Terminal.ProfilesList")

    @property
    def profile_uuids(self) -> set[UUID]:
        """Get all profile UUIDs from gsettings."""
        return {UUID(x) for x in self.settings.get_strv("list")}

    @profile_uuids.setter
    def profile_uuids(self, uuids: set[UUID]) -> None:
        """Set the profile UUIDs in gsettings."""
        self.settings.set_strv("list", [str(uuid) for uuid in uuids])

    @property
    def default_profile_uuid(self) -> UUID:
        """Get the default profile UUID from gsettings."""
        return UUID(self.settings.get_string("default"))


def gsettings_set_default_profile_uuid(profile_uuid: UUID) -> None:
    GSETTINGS_PROFILES_LIST.set_string("default", str(profile_uuid))


class ProfileDConf:
    def __init__(self, profile_uuid: UUID):
        self.profile_uuid = profile_uuid

    def _path(self, property_name: str) -> str:
        """Construct the dconf path for the given property."""
        return f"/org/gnome/terminal/legacy/profiles:/:{self.profile_uuid}/{property_name}"

    def read_property(self, property_name: str) -> str | bool:
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
            raise ValueError(f"Unsupported dconf value type: {type(value)}")
        except subprocess.CalledProcessError as e:
            raise KeyError(f"Reading '{property_name}' from {self.profile_uuid} failed") from e

    @property
    def visible_name(self) -> str:
        v = self.read_property("visible-name")
        assert isinstance(v, str)
        return v

    @visible_name.setter
    def visible_name(self, name: str) -> None:
        assert isinstance(name, str), "Name must be a string"
        self.write_property("visible-name", name)

    def write_property(self, property_name: str, value: bool | str) -> None:
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
            raise ValueError(f"Unsupported value type: {type(value)}. Must be bool or str.")
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


def get_profile_uuid_by_name_mapping() -> dict[str, UUID]:
    """Build a mapping of profile names to UUIDs."""
    uuid_by_name: dict[str, UUID] = {}

    for profile_uuid in GSettingsProfiles().profile_uuids:
        try:
            name = ProfileDConf(profile_uuid).visible_name
        except (KeyError, AssertionError, ValueError) as e:
            logging.warning(f"Cannot get name for profile {profile_uuid}: {e}")
            continue
        if name in uuid_by_name:
            logging.warning(f"Duplicate profile {name}: {uuid_by_name[name]} and {profile_uuid}")
            continue
        uuid_by_name[name] = profile_uuid

    return uuid_by_name


def create_auto_profile():
    # Create new profile
    auto_uuid = uuid.uuid4()

    # Add to profile list in gsettings
    gsettings_profiles = GSettingsProfiles()
    gsettings_profiles.profile_uuids = gsettings_profiles.profile_uuids | {auto_uuid}

    # Set profile name
    auto_dconf = ProfileDConf(auto_uuid)
    auto_dconf.visible_name = AUTO_PROFILE_NAME
    return auto_uuid


def create_or_update_auto_profile(source_profile_name: str) -> UUID:
    """Create or update the auto profile with colors from the specified theme."""
    uuid_by_name = get_profile_uuid_by_name_mapping()

    # Check if auto profile exists
    auto = [name for name in uuid_by_name if name.startswith(AUTO_PROFILE_NAME)]
    if len(auto) > 1:
        raise ValueError(f"Multiple Auto profiles found: {auto}")
    if len(auto) == 1:
        auto_uuid = uuid_by_name[auto[0]]
        logging.info(f"Found existing Auto profile: {auto_uuid}")
    else:
        auto_uuid = create_auto_profile()
        logging.info(f"Created new Auto profile: {auto_uuid}")

    logging.info(f"Applying {source_profile_name} colors to Auto profile")
    copy_profile_colors(uuid_by_name[source_profile_name], auto_uuid)
    ProfileDConf(auto_uuid).write_property("visible-name", f"{AUTO_PROFILE_NAME} ({source_profile_name})")

    return auto_uuid


def dbus_update_profile_on_all_windows(new_uuid: UUID) -> None:
    bus = dbus.SessionBus()

    obj = bus.get_object("org.gnome.Terminal", "/org/gnome/Terminal/window")
    iface = dbus.Interface(obj, "org.freedesktop.DBus.Introspectable")

    tree = ElementTree.fromstring(iface.Introspect())
    windows = [child.attrib["name"] for child in tree if child.tag == "node"]
    logging.info(f"requesting new uuid: {new_uuid}")

    def _get_window_profile_uuid(window_actions_iface):
        description = window_actions_iface.Describe("profile")
        return UUID(description[2][0])

    for window in windows:
        obj = bus.get_object("org.gnome.Terminal", f"/org/gnome/Terminal/window/{window}")
        window_actions_iface = dbus.Interface(obj, "org.gtk.Actions")
        original_uuid = _get_window_profile_uuid(window_actions_iface)
        logging.info(f"talking to {obj}, starting profile uuid: {original_uuid}")
        window_actions_iface.SetState("profile", str(new_uuid), [])
        uuid_after = _get_window_profile_uuid(window_actions_iface)
        logging.info(f"new uuid after action: {uuid_after}")
        assert uuid_after == new_uuid


def _main(_):
    # Create or update the auto profile with colors from the profile
    auto_uuid = create_or_update_auto_profile(_PROFILE.value)
    # Set as default
    gsettings_set_default_profile_uuid(auto_uuid)


def main():
    # Entrypoint used by the console_script. Enforce required flag and run.
    flags.mark_flag_as_required(_PROFILE.name)
    app.run(_main)
