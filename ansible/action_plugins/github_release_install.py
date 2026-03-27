"""
GitHub Release Install Action Plugin

Installs software from GitHub releases with integrated release information lookup.

Features:
- Pure Python implementation (no external commands)
- Multiple installation methods (deb, binary, archive)
- Integrated GitHub release information lookup
- Version acknowledgment system for controlling upgrades

Usage:
  github_release_install:
    # Required parameters
    repo: "owner/repo"  # GitHub repository

    # Optional common parameters
    release_spec:
      version: "v1.0.0|latest"          # Version to install (defaults to latest)
      asset_pattern: ".*amd64\\.deb$"   # Regex pattern to select asset
      acknowledged_version: "v1.0.0"    # Last version seen/acknowledged by user

    # Optional release data from previous task
    release_data: "{{ release_data }}"

    method: deb  # Installation method (deb, binary, archive)

    method:
      name: binary
      dest_path: /usr/local/bin/app   # Path where binary will be installed

    method:
      name: archive
      dest_path: "/opt/app"         # Directory where archive will be extracted
      creates_file: "/opt/app/bin"  # Optional path that should exist after install

    method:
      name: archive
      dest_path: "/usr/local/bin/tool"  # Where to install extracted file
      extract_file: "tool"              # Extract only this file from archive

Example:
  # One-step installation process
  - name: Install AppImageLauncher
    github_release_install:
      repo: "TheAssassin/AppImageLauncher"
      method: deb
      version: "v3.0.0-alpha-4"
      acknowledged_version: "v3.0.0-alpha-4"
      asset_pattern: ".*_{{ common_dpkg_arch }}\\.deb$"
    become: true

  # Separate release info gathering
  - name: Show available releases
    github_release_install:
      repo: "TheAssassin/AppImageLauncher"
      method: deb
      version: "latest"
      asset_pattern: ".*_{{ common_dpkg_arch }}\\.deb$"
    register: release_data
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

from ansible.plugins.action import ActionBase

# Add the parent directory to the path so we can use sibling modules
plugins_dir = str(Path(__file__).parent.parent)
if plugins_dir not in sys.path:
    sys.path.insert(0, plugins_dir)

from module_utils.github_release import INSTALL_METHODS, ActionError, GitHubInstaller, ReleaseSpec, _fail  # noqa: E402

ENSURE_ABSENT = "absent"
ENSURE_PRESENT = "present"


class ActionModule(ActionBase):
    """GitHub Release Install action plugin."""

    def _handle_archive_extract_file(self, args: dict[str, Any], task_vars: dict, tmp: Any) -> dict[str, Any]:
        """Handle extraction of a specific file from an archive.

        This performs a multi-step process:
        1. Download archive to temp location
        2. Extract to temp directory
        3. Copy specific file to destination
        4. Clean up temp files
        """

        asset_url = args["asset_url"]
        extract_file = args["extract_file"]
        dest_path = args["dest_path"]

        # Use ansible temp dir if available, otherwise system temp
        temp_base = tmp or tempfile.gettempdir()
        base_path = Path(temp_base)
        temp_archive = str(base_path / "github_release_archive.tar.gz")
        temp_extract_dir = str(base_path / "github_release_extract")

        result = {"changed": False}

        # Step 1: Download archive
        download_result = self._execute_module(
            module_name="ansible.builtin.get_url",
            module_args={"url": asset_url, "dest": temp_archive, "mode": "0644"},
            task_vars=task_vars,
            tmp=tmp,
        )
        if download_result.get("failed"):
            return download_result

        # Step 2: Create temp directory and extract archive
        mkdir_result = self._execute_module(
            module_name="ansible.builtin.file",
            module_args={"path": temp_extract_dir, "state": "directory", "mode": "0755"},
            task_vars=task_vars,
            tmp=tmp,
        )
        if mkdir_result.get("failed"):
            # Clean up downloaded archive
            self._execute_module(
                module_name="ansible.builtin.file",
                module_args={"path": str(temp_archive), "state": "absent"},
                task_vars=task_vars,
                tmp=tmp,
            )
            return mkdir_result

        extract_result = self._execute_module(
            module_name="ansible.builtin.unarchive",
            module_args={"src": temp_archive, "dest": temp_extract_dir, "remote_src": True},
            task_vars=task_vars,
            tmp=tmp,
        )
        if extract_result.get("failed"):
            # Clean up downloaded archive
            self._execute_module(
                module_name="ansible.builtin.file",
                module_args={"path": str(temp_archive), "state": "absent"},
                task_vars=task_vars,
                tmp=tmp,
            )
            return extract_result

        # Step 3: Copy specific file to destination
        source_file = str(Path(temp_extract_dir) / extract_file)
        copy_result = self._execute_module(
            module_name="ansible.builtin.copy",
            module_args={"src": source_file, "dest": dest_path, "mode": "0755", "remote_src": True},
            task_vars=task_vars,
            tmp=tmp,
        )

        # Step 4: Clean up temp files
        for path in (temp_archive, temp_extract_dir):
            self._execute_module(
                module_name="ansible.builtin.file",
                module_args={"path": str(path), "state": "absent"},
                task_vars=task_vars,
                tmp=tmp,
            )

        if copy_result.get("failed"):
            return copy_result

        result["changed"] = any(
            [
                download_result.get("changed", False),
                extract_result.get("changed", False),
                copy_result.get("changed", False),
            ]
        )

        return result

    def _create_installer(self, args: dict[str, Any]) -> GitHubInstaller:
        """Create installer instance based on method.

        Raises:
            ActionError: If installer creation fails
        """
        # Pass all arguments to the installer class, which will extract what it needs
        # The installer class constructor is responsible for validating required params
        if not (method := args.get("method")):
            raise ActionError("Missing required parameter: method")

        if isinstance(method, str):
            method_name, method_args = method, {}
        elif isinstance(method, dict):
            method_name, method_args = method.get("name"), method.copy()
            method_args.pop("name")
        else:
            raise ActionError(f"Invalid {type(method) = }.")
        assert isinstance(method_name, str)
        if not (klass := INSTALL_METHODS.get(method_name)):
            raise ActionError(f"Invalid {method = }. Expected one of: {', '.join(INSTALL_METHODS.keys())}")
        installer = klass(**method_args)
        installer.validate()
        return installer

    def run(self, tmp=None, task_vars=None):
        """Main entry point for the action plugin."""
        if task_vars is None:
            task_vars = {}

        result = super().run(tmp, task_vars)
        result.update(changed=False, failed=False)

        args = self._task.args.copy()

        # Check if we have release_data from a previous task
        if "release_data" not in args:
            # todo dedupe
            if "release_spec" not in args:
                return _fail(result, "Missing required parameter: release_data xor release_spec")
            release_data = ReleaseSpec(**args["release_spec"]).resolve()
            if release_data.get("failed"):
                # For version acknowledgment failures, return a clean result
                if "acknowledged_version" in release_data:
                    return {
                        "failed": True,
                        "msg": release_data["msg"],
                        "latest_version": release_data.get("latest_version"),
                        "acknowledged_version": release_data.get("acknowledged_version"),
                        "repo": release_data.get("repo"),
                    }
                return _fail(result, release_data["msg"])
            result["release_data"] = release_data
        # todo dedupe
        elif not (release_data := args.get("release_data")):
            return _fail(result, "Missing required parameter: release_data xor release_spec")
        if not (asset_url := release_data.get("asset_url")):
            return _fail(result, "No asset URL in release info")

        # Install the release
        try:
            installer = self._create_installer(args)
        except ActionError as e:
            return _fail(result, str(e))

        # Handle special case for archive extraction with specific file
        if installer.module_name == "_multi_step_archive_extract":
            install_result = self._handle_archive_extract_file(installer.install_module_args(asset_url), task_vars, tmp)
        else:
            install_result = self._execute_module(
                module_name=installer.module_name,
                module_args=installer.install_module_args(asset_url),
                task_vars=task_vars,
                tmp=tmp,
            ) | installer.get_additional_info(asset_url)

        result["install_result"] = install_result

        if install_result.get("failed"):
            return _fail(result, install_result["msg"])

        return result | {"changed": install_result.get("changed", False)}
