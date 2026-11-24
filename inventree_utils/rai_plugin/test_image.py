"""Verify the shipped InvenTree image can discover and load RaiPlugin."""

import subprocess

import pytest_bazel

from devinfra.oci.oci import load_oci_image
from inventree_utils.rai_plugin import test_image_layout

_SCRIPT = """
from importlib.metadata import entry_points
import sys

from plugin import InvenTreePlugin, registry

matches = list(entry_points(group="inventree_plugins", name="rai_plugin"))
assert len(matches) == 1, f"{matches=}, {sys.executable=}, {sys.path=}"

plugin_class = matches[0].load()
assert issubclass(plugin_class, InvenTreePlugin)
assert plugin_class.SLUG == "raiplugin"
assert plugin_class in registry.collect_plugins()
"""


def test_image_discovers_rai_plugin() -> None:
    image = load_oci_image(test_image_layout.LAYOUT_RLOCATION)
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--env=INVENTREE_DB_ENGINE=sqlite3",
            "--env=INVENTREE_DB_NAME=/tmp/inventree.sqlite3",
            "--env=INVENTREE_PLUGINS_ENABLED=true",
            "--env=INVENTREE_SITE_URL=http://localhost:8000",
            image,
            "/bin/sh",
            "-c",
            'cd "$INVENTREE_BACKEND_DIR/InvenTree" && exec python3 manage.py shell -c "$1"',
            "probe",
            _SCRIPT,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    pytest_bazel.main()
