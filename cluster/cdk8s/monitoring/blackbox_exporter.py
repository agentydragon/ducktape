"""Blackbox exporter image deployed by both monitoring probes and exercised by the exporter test."""

import json

from python.runfiles import Runfiles

_pin = json.loads((Runfiles.CreateOrRaise().root() / "ducktape/devinfra/image_pins.json").read_bytes())[
    "blackbox_exporter"
]
IMAGE = f"{_pin['image']}:{_pin['tag']}@{_pin['digest']}"
