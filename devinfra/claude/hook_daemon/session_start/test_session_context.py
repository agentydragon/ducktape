"""Snapshot tests for the session_context.mako template."""

import logging
from collections.abc import Callable
from pathlib import Path

import pytest
import pytest_bazel
from syrupy.assertion import SnapshotAssertion

from devinfra.claude.auth_proxy import setup as proxy_setup
from devinfra.claude.hook_daemon import templates
from devinfra.claude.hook_daemon.config import BackgroundCommand, ProfileConfig
from devinfra.claude.hook_daemon.models import StartupResult
from devinfra.claude.hook_daemon.session_start import container_runtime, platform_detect
from devinfra.claude.hook_daemon.session_start.handler import LogCollector
from devinfra.claude.hook_daemon.testing.testing_helpers import TEST_PROFILE


def _render(
    *,
    platform: platform_detect.PlatformInfo,
    proxy: proxy_setup.ProxyConfigured | None = None,  # NoProxy | ProxyConfigured | None
    container: container_runtime.ContainerRuntimeSetup | None = None,
    background_commands: list[BackgroundCommand] | None = None,
    extra_context: str = "",
    log_entries: list[logging.LogRecord] | None = None,
    log_file: str = "/tmp/daemon.log",
    buildbuddy_configured: bool = False,
    profile: ProfileConfig = TEST_PROFILE,
    session_id: str = "test-session-id",
    startup: StartupResult | None = None,
) -> str:
    collector = LogCollector()
    collector.buffer.extend(log_entries or [])
    return str(
        templates.session_context.render(
            collector=collector,
            proxy=proxy,
            container=container,
            background_commands=background_commands or [],
            extra_context=extra_context,
            log_file=log_file,
            buildbuddy_configured=buildbuddy_configured,
            platform=platform,
            profile=profile,
            bazel_remote_proxy_sock=None,
            session_id=session_id,
            startup=startup or StartupResult(),
        )
    )


@pytest.fixture
def cli_platform() -> Callable[..., platform_detect.PlatformInfo]:
    def _make(*, nix_installed: bool = False, nixpkgs_available: bool = False) -> platform_detect.PlatformInfo:
        return platform_detect.PlatformInfo(
            hostname="wyrm2",
            root_fstype="ext4",
            init_cmdline=["/sbin/init"],
            kernel_version="6.12.0",
            platform=platform_detect.WebPlatform.UNKNOWN,
            nix_installed=nix_installed,
            nixpkgs_available=nixpkgs_available,
        )

    return _make


@pytest.fixture
def web_platform() -> platform_detect.PlatformInfo:
    return platform_detect.PlatformInfo(
        hostname="runsc",
        root_fstype="9p",
        init_cmdline=["--firecracker-init"],
        kernel_version="5.15.0",
        platform=platform_detect.WebPlatform.FIRECRACKER,
        nix_installed=False,
        nixpkgs_available=False,
    )


@pytest.fixture
def proxy() -> proxy_setup.ProxyConfigured:
    return proxy_setup.ProxyConfigured(combined_ca=Path("/session/auth-proxy/combined_ca.pem"))


# === CLI mode ===


def test_cli_no_nix(snapshot: SnapshotAssertion, cli_platform: Callable[..., platform_detect.PlatformInfo]) -> None:
    result = _render(platform=cli_platform())
    assert result == snapshot


def test_cli_nix_with_nixpkgs(
    snapshot: SnapshotAssertion, cli_platform: Callable[..., platform_detect.PlatformInfo]
) -> None:
    result = _render(platform=cli_platform(nix_installed=True, nixpkgs_available=True))
    assert result == snapshot


def test_cli_nix_without_nixpkgs(
    snapshot: SnapshotAssertion, cli_platform: Callable[..., platform_detect.PlatformInfo]
) -> None:
    result = _render(platform=cli_platform(nix_installed=True, nixpkgs_available=False))
    assert result == snapshot


def test_cli_with_buildbuddy(
    snapshot: SnapshotAssertion, cli_platform: Callable[..., platform_detect.PlatformInfo]
) -> None:
    result = _render(platform=cli_platform(), buildbuddy_configured=True)
    assert result == snapshot


# === Web mode ===


def test_web_no_nix(
    snapshot: SnapshotAssertion, web_platform: platform_detect.PlatformInfo, proxy: proxy_setup.ProxyConfigured
) -> None:
    result = _render(platform=web_platform, proxy=proxy, buildbuddy_configured=True)
    assert result == snapshot


def test_web_with_docker(
    snapshot: SnapshotAssertion, web_platform: platform_detect.PlatformInfo, proxy: proxy_setup.ProxyConfigured
) -> None:
    result = _render(
        platform=web_platform,
        proxy=proxy,
        container=container_runtime.ContainerRuntimeSetup(
            socket_url="unix:///var/run/docker.sock", status="running", storage_driver="overlay"
        ),
        buildbuddy_configured=True,
    )
    assert result == snapshot


def test_web_with_background_commands(
    snapshot: SnapshotAssertion, web_platform: platform_detect.PlatformInfo, proxy: proxy_setup.ProxyConfigured
) -> None:
    cmds = [
        BackgroundCommand(name="apt package install", command="apt-get install -y foo"),
        BackgroundCommand(name="bazel info", command="bazelisk info", after_env=True),
    ]
    result = _render(platform=web_platform, proxy=proxy, background_commands=cmds)
    assert result == snapshot


def test_with_warnings_in_log(
    snapshot: SnapshotAssertion, cli_platform: Callable[..., platform_detect.PlatformInfo]
) -> None:
    record = logging.LogRecord(
        name="session_start",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="Something went wrong during setup",
        args=(),
        exc_info=None,
    )
    result = _render(platform=cli_platform(), log_entries=[record])
    assert result == snapshot


def test_startup_script_succeeded(
    snapshot: SnapshotAssertion, cli_platform: Callable[..., platform_detect.PlatformInfo]
) -> None:
    startup = StartupResult(
        exit_code=0,
        output="secrets: BUILDBUDDY_API_KEY: OK\nkubeconfig: wrote ~/.kube/config\nNix: available with nixpkgs.",
        env_overlay={"BUILDBUDDY_API_KEY": "key123", "GITHUB_TOKEN": "ghp_xxx"},
    )
    result = _render(platform=cli_platform(), startup=startup)
    assert result == snapshot


def test_startup_script_failed(
    snapshot: SnapshotAssertion, cli_platform: Callable[..., platform_detect.PlatformInfo]
) -> None:
    startup = StartupResult(
        exit_code=1,
        output="WARNING: secrets: BUILDBUDDY_API_KEY: sops decrypt failed: age: no identity matched",
        env_overlay={},
    )
    result = _render(platform=cli_platform(), startup=startup)
    assert result == snapshot


if __name__ == "__main__":
    pytest_bazel.main()
