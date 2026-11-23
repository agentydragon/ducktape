"""Pytest fixtures for rendering a packaged extension in GNOME Shell."""

import os
import zipfile
from pathlib import Path

import pytest
from python.runfiles import Runfiles

from devinfra.oci.oci import load_oci_image
from gnome.test_image import gnome_shell_test


@pytest.fixture(scope="module")
def gnome_shell_test_image() -> str:
    return load_oci_image(gnome_shell_test.LAYOUT_RLOCATION)


@pytest.fixture(scope="module")
def extension_dir(runfiles: Runfiles, tmp_path_factory: pytest.TempPathFactory) -> Path:
    dest = tmp_path_factory.mktemp("gnome-extension")
    with zipfile.ZipFile(runfiles.root() / os.environ["GNOME_EXTENSION_ZIP"]) as archive:
        archive.extractall(dest)
    return dest
