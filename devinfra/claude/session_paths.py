"""Session-scoped path computations.

All paths derived from a session_id live here. This is a plain dataclass
(not pydantic-settings) — it computes paths, not config.
"""

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir


@dataclass(frozen=True)
class SessionPaths:
    """All session-scoped paths, derived from session_id + resolved home/cache roots."""

    session_id: str
    home: Path
    xdg_cache_home: Path

    @classmethod
    def from_env(cls, session_id: str, env: dict[str, str]) -> "SessionPaths":
        """Construct from an environment dict, resolving home/cache eagerly."""
        home = Path(env["HOME"]) if "HOME" in env else Path.home()
        xdg_cache_home = Path(
            env["XDG_CACHE_HOME"] if "XDG_CACHE_HOME" in env else user_cache_dir(appname="claude-hooks")
        )
        return cls(session_id=session_id, home=home, xdg_cache_home=xdg_cache_home)

    @property
    def session_dir(self) -> Path:
        return self.home / ".claude" / "session-env" / self.session_id

    @property
    def cache_dir(self) -> Path:
        """Base cache directory for claude-hooks (auto-created)."""
        d = self.xdg_cache_home
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def config_dir(self) -> Path:
        """Base config directory for claude-hooks (auto-created)."""
        return Path(user_config_dir(appname="claude-hooks", ensure_exists=True))

    @property
    def supervisor_dir(self) -> Path:
        return self.session_dir / "supervisor"

    @property
    def supervisor_pidfile(self) -> Path:
        return self.supervisor_dir / "supervisord.pid"

    @property
    def auth_proxy_dir(self) -> Path:
        return self.session_dir / "auth-proxy"

    @property
    def auth_proxy_combined_ca(self) -> Path:
        """Combined CA bundle (system CAs + proxy CA)."""
        return self.auth_proxy_dir / "combined_ca.pem"

    @property
    def auth_proxy_creds_file(self) -> Path:
        """Upstream proxy credentials file (lives in hook daemon dir, read by in-process proxy)."""
        return self.hook_daemon_dir / "upstream_proxy"

    @property
    def auth_proxy_ca_file(self) -> Path:
        """Extracted Anthropic CA file."""
        return self.auth_proxy_dir / "anthropic_ca.pem"

    @property
    def auth_proxy_truststore(self) -> Path:
        """Java truststore with proxy CA."""
        return self.auth_proxy_dir / "cacerts.jks"

    @property
    def bazelisk_path(self) -> Path:
        """Bazelisk binary path (global cache, not session-scoped)."""
        return self.cache_dir / "bazelisk"

    @property
    def wrapper_dir(self) -> Path:
        """Wrapper directory (added to PATH)."""
        return self.session_dir / "bin"

    @property
    def wrapper_path(self) -> Path:
        """Wrapper script path."""
        return self.wrapper_dir / "bazel"

    @property
    def mkcert_dir(self) -> Path:
        """mkcert directory for certs and CA (session-scoped)."""
        return self.session_dir / "mkcert"

    @property
    def mkcert_binary(self) -> Path:
        """mkcert binary path (global cache, not session-scoped)."""
        return self.cache_dir / "mkcert"

    @property
    def podman_dir(self) -> Path:
        return self.session_dir / "podman"

    @property
    def docker_dir(self) -> Path:
        return self.session_dir / "docker"

    @property
    def container_storage_dir(self) -> Path:
        """Tmpfs-backed storage root for the active container runtime."""
        return self.session_dir / "container-storage"

    @property
    def sandbox_writable_dir(self) -> Path:
        """Directory writable from within Claude Code's sandbox.

        Claude Code's Bash tool sandbox makes ~/.claude/session-env/ read-only,
        so runtime writes (e.g. bazel-wrapper log) must go to /tmp/claude/.
        """
        return Path("/tmp/claude") / self.session_id

    @property
    def bazel_cache_dir(self) -> Path:
        """Bazel cache directory (tmpfs-backed, via startup --output_user_root)."""
        return self.session_dir / "bazel-cache"

    @property
    def hook_daemon_dir(self) -> Path:
        """Runtime directory for hook daemon (socket, pidfile, logs)."""
        return self.session_dir / "hook-daemon"

    @property
    def hook_daemon_sock(self) -> Path:
        """UDS path for the hook daemon.

        Uses a short path under /tmp to stay within the 108-byte AF_UNIX limit.
        The parent directory is created by ensure_dirs(), not on every access.
        """
        return Path("/tmp/claude-hd") / self.session_id / "d.sock"

    def ensure_dirs(self) -> None:
        """Create all directories that must exist before use (socket dir, session dir, etc.)."""
        self.hook_daemon_sock.parent.mkdir(parents=True, exist_ok=True)

    @property
    def hook_daemon_pidfile(self) -> Path:
        """PID file for the hook daemon process."""
        return self.hook_daemon_dir / "daemon.pid"

    @property
    def hook_daemon_env_file(self) -> Path:
        """Persisted session env (written by daemon on each request)."""
        return self.hook_daemon_dir / "session_env.json"
