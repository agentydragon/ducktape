"""
GitHub Release Utility Module

This module provides shared functionality for GitHub release actions and modules.
It contains common classes and functions used by the GitHub release action
plugins.
"""

import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Any


class ActionError(Exception):
    """Custom exception for action module errors."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _fail(result, msg: str) -> dict[str, Any]:
    return result | {"failed": True, "msg": msg}


def _prune_dict(x: dict[str, Any], keys: set[str]):
    """Remove specified keys from a dictionary."""
    for key in keys & x.keys():
        del x[key]


@dataclass
class ReleaseSpec:
    """Base class for GitHub release information."""

    repo: str | None = None
    version: str = "latest"
    asset_pattern: str | None = None
    acknowledged_version: str | None = None

    def resolve(self) -> dict[str, Any]:
        """Gets GitHub release info."""
        result: dict[str, Any] = {}

        # Make API request
        try:
            req = urllib.request.Request(
                self.get_api_url(),
                headers={"Accept": "application/json", "User-Agent": "Ansible GitHub Release Handler"},
            )
            with urllib.request.urlopen(req) as response:
                release_data = json.loads(response.read().decode("utf-8"))
        except Exception as e:
            return _fail(result, f"Error fetching release info: {e!s}")

        # Clear known-unused keys
        for asset in release_data.get("assets", []):
            # explicitly kept: 'label', 'name', 'url'
            _prune_dict(
                asset,
                {
                    "content_type",
                    "created_at",
                    "download_count",
                    "id",
                    "node_id",
                    "size",
                    "state",
                    "updated_at",
                    "uploader",
                    "zipball_url",
                    "tarball_url",
                    "url",  # API URL; we use browser_download_url
                },
            )
        _prune_dict(
            release_data,
            {
                "author",
                "body",
                "created_at",
                "draft",
                "html_url",
                "id",
                "node_id",
                "prerelease",
                "published_at",
                "reactions",
            },
        )
        result["release_data"] = release_data

        # Handle acknowledged version if provided
        if self.acknowledged_version:
            latest_version = release_data["tag_name"]
            if not latest_version:
                return _fail(result, "Failed to extract version information of latest release.")
            result["latest_version"] = latest_version

            if self.acknowledged_version != latest_version:
                # Create a minimal failure result with just essential info
                return {
                    "failed": True,
                    "msg": (
                        f"Please acknowledge new version {latest_version}. "
                        f"Last acknowledged: {self.acknowledged_version}. "
                        f"Check https://github.com/{self.repo}/releases"
                    ),
                    "latest_version": latest_version,
                    "acknowledged_version": self.acknowledged_version,
                    "repo": self.repo,
                }

        if not (assets := release_data.get("assets")):
            return _fail(result, "No assets found in release data.")
        if not self.asset_pattern:
            return _fail(result, "No asset pattern provided.")
        matches = [asset for asset in assets if re.search(self.asset_pattern, asset["name"])]
        if len(matches) > 1:
            return _fail(result, f"{len(matches)} assets match {self.asset_pattern}. Use a more specific pattern.")
        if not matches:
            available = ", ".join(asset["name"] for asset in assets)
            return _fail(result, f"No assets match {self.asset_pattern}. Available: {available}")
        if not (url := matches[0].get("browser_download_url")):
            return _fail(result, "No download URL found for the asset.")
        return {"asset_url": url}

    def get_api_url(self) -> str:
        """Return GitHub API URL for the release."""
        url = f"https://api.github.com/repos/{self.repo}/releases"
        if self.version != "latest":
            url += f"/tags/{self.version}"
        else:
            url += "/latest"
        return url


@dataclass
class GitHubInstaller:
    """Base for GitHub release installers."""

    @property
    def module_name(self) -> str:
        """Ansible module name for this installation method."""
        raise NotImplementedError

    def install_module_args(self, asset_url: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_additional_info(self, asset_url: str) -> dict[str, Any]:
        """Extra information about installed asset."""
        return {}

    def validate(self) -> None:
        pass


@dataclass
class DebInstall(GitHubInstaller):
    """Install a GitHub release as a Debian package."""

    @property
    def module_name(self) -> str:
        return "ansible.builtin.apt"

    def install_module_args(self, asset_url: str) -> dict[str, Any]:
        return {"deb": asset_url}


@dataclass
class BinaryInstall(GitHubInstaller):
    """Install a GitHub release as a binary executable."""

    dest_path: str | None = None

    @property
    def module_name(self) -> str:
        return "ansible.builtin.get_url"

    def install_module_args(self, asset_url: str) -> dict[str, Any]:
        """Return arguments for installing the binary."""
        return {"url": asset_url, "dest": self.dest_path, "mode": "0755"}

    def validate(self) -> None:
        super().validate()
        if not self.dest_path:
            raise ActionError("dest_path is required for binary installation")


ARCHIVES = (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar.zst", ".zip")


@dataclass
class ArchiveInstall(GitHubInstaller):
    """Install a GitHub release from an archive file."""

    dest_path: str | None = None
    creates_file: str | None = None
    extract_file: str | None = None  # Optional: extract only this file from archive

    @property
    def module_name(self) -> str:
        # When extracting a specific file, we need to handle it differently
        if self.extract_file:
            return "_multi_step_archive_extract"
        return "ansible.builtin.unarchive"

    def install_module_args(self, asset_url: str) -> dict[str, Any]:
        """Return arguments for extracting and installing the archive."""
        if self.extract_file:
            # For single file extraction, we'll handle this in the action plugin
            return {"asset_url": asset_url, "extract_file": self.extract_file, "dest_path": self.dest_path}

        # Normal full archive extraction
        args = {"src": asset_url, "dest": self.dest_path, "remote_src": True}
        if self.creates_file:
            args["creates"] = self.creates_file
        return args

    def validate(self) -> None:
        super().validate()
        if not self.dest_path:
            raise ActionError("dest_path is required for archive installation")
        if self.extract_file and self.creates_file:
            raise ActionError("creates_file cannot be used with extract_file")

    def get_additional_info(self, asset_url: str) -> dict[str, Any]:
        """Determine extracted directory name and pattern from asset URL."""
        filename = asset_url.split("/")[-1]
        # Extract base name without extension(s)
        for ext in ARCHIVES:
            if filename.endswith(ext):
                return {"extracted_dir": filename.removesuffix(ext)}
        return _fail({}, f"Can't guess extracted directory from URL: {asset_url}")


# Maps method name to implementation.
INSTALL_METHODS: dict[str, type[GitHubInstaller]] = {
    "deb": DebInstall,
    "binary": BinaryInstall,
    "archive": ArchiveInstall,
}
