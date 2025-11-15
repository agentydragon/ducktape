#!/usr/bin/env python3
"""
Requires:
    sudo apt install libglib2.0-dev-bin gir1.2-glib-2.0
"""

import argparse
import json
import logging
from pathlib import Path
import sys
import uuid

from gi.repository import Gio

SCHEMA_LIST = "org.gnome.Terminal.ProfilesList"
PROFILE_BASE = "/org/gnome/terminal/legacy/profiles:/"

logger = logging.getLogger(__name__)


class SettingsWrapper:
    def __init__(self, schema: str, path: str):
        self.path = path
        self.settings = Gio.Settings.new_with_path(schema, path)
        self._changed_keys = set()

    def __setitem__(self, key: str, val):
        prefix = f"{self.path}.{key}:"
        if (old := self.settings[key]) == val:
            logger.debug(f"{prefix} {old!r} already")
            return
        logger.debug(f"{prefix} {old!r} → {val!r}")
        # NOTE: set_* throws TypeError on wrong types; we don't
        # need to check ourselves.
        self.settings[key] = val
        self._changed_keys.add(key)

    def __getitem__(self, key: str):
        return self.settings[key]

    @property
    def changed(self) -> bool:
        return bool(self._changed_keys)

    def sync(self) -> None:
        self.settings.sync()


class Profile(SettingsWrapper):
    def __init__(self, uuid_: str):
        self.uuid = uuid_
        super().__init__("org.gnome.Terminal.Legacy.Profile", f"{PROFILE_BASE}:{uuid_}/")

    def apply_color_scheme(self, name: str, color_dir: Path, font: str | None = None) -> None:
        self["visible-name"] = name
        self["background-color"] = (color_dir / "bg_color").read_text().strip()
        self["foreground-color"] = (color_dir / "fg_color").read_text().strip()
        self["bold-color"] = (color_dir / "bd_color").read_text().strip()
        self["cursor-colors-set"] = False
        self["use-theme-colors"] = False
        self["bold-color-same-as-fg"] = False
        self["palette"] = (color_dir / "palette").read_text().splitlines()

        if font:
            self["font"] = font
            self["use-system-font"] = False
        else:
            self["use-system-font"] = True
        self.settings.sync()


class ProfileList(SettingsWrapper):
    def __init__(self):
        super().__init__(SCHEMA_LIST, PROFILE_BASE)

    @property
    def default(self) -> str:
        return self["default"]

    @default.setter
    def default(self, uuid_: str) -> None:
        self["default"] = uuid_

    def find_profile_by_name(self, name: str) -> Profile | None:
        profiles = [Profile(u) for u in self["list"]]
        matches = [p for p in profiles if p["visible-name"] == name]
        if len(matches) > 1:
            raise RuntimeError(f"Multiple '{name}' profiles: {', '.join(p.uuid for p in matches)}")
        return matches[0] if matches else None

    def create_profile(self) -> Profile:
        uuids = self["list"]
        new_uuid = str(uuid.uuid4())
        logger.debug(f"Existing uuids: {uuids!r}, add new uuid {new_uuid}")
        self["list"] = [*uuids, new_uuid]
        return Profile(new_uuid)


def cmd_apply(name: str, color_dir: Path, font: str | None):
    logger.debug(f"cmd_apply: {name=}, {color_dir=}, {font=}")
    profile_list = ProfileList()

    if not (profile := profile_list.find_profile_by_name(name)):
        profile = profile_list.create_profile()
        profile_list.sync()
        logger.debug("Created profile, applying settings")
    else:
        logger.debug(f"Existing profile '{name}': {profile.uuid}")

    profile.apply_color_scheme(name, color_dir, font=font)
    profile.sync()

    return {"changed": profile.changed, "uuid": profile.uuid}


def cmd_set_default(name: str):
    profile_list = ProfileList()
    if not (profile := profile_list.find_profile_by_name(name)):
        raise RuntimeError(f"Profile '{name}' not found.")
    profile_list["default"] = profile.uuid
    profile_list.sync()

    return {"changed": profile_list.changed}


def main():
    # Define a parent parser with global options
    global_opts = argparse.ArgumentParser(add_help=False)
    global_opts.add_argument("--debug", action="store_true", help="Enable debug logging")

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # Subcommand: apply
    apply_parser = subparsers.add_parser("apply", parents=[global_opts])
    apply_parser.add_argument("name")
    apply_parser.add_argument("color_dir", type=Path)
    apply_parser.add_argument("--font", help="Terminal font, e.g. 'MesloLGS 12'")

    # Subcommand: set-default
    set_default_parser = subparsers.add_parser("set-default", parents=[global_opts])
    set_default_parser.add_argument("name")

    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, format="%(message)s")

    if args.mode == "apply":
        output = cmd_apply(args.name, args.color_dir, font=args.font)
    elif args.mode == "set-default":
        output = cmd_set_default(args.name)
    else:
        raise RuntimeError(f"Unknown {args.mode = }")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
