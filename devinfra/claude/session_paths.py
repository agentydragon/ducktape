"""Session-scoped path computations.

All paths derived from a session_id live here. This is a plain class
(not pydantic-settings) — it computes paths, not config.
"""

from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir


class SessionPaths:
    """All session-scoped paths, derived from session_id.

    When constructed inside a long-lived daemon, pass the caller's HOME and
    XDG dirs via ``from_env()`` so paths resolve against the caller's
    environment rather than the daemon's.
    """

    def __init__(self, session_id: str, *, home: Path | None = None, xdg_cache_home: Path | None = None) -> None:
        self._session_id = session_id
        self._home = home
        self._xdg_cache_home = xdg_cache_home

    @classmethod
    def from_env(cls, session_id: str, env: dict[str, str]) -> "SessionPaths":
        """Construct from a caller's environment dict (daemon use-case)."""
        home = Path(env["HOME"]) if "HOME" in env else None
        xdg_cache_home = Path(env["XDG_CACHE_HOME"]) if "XDG_CACHE_HOME" in env else None
        return cls(session_id, home=home, xdg_cache_home=xdg_cache_home)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def _resolved_home(self) -> Path:
        return self._home if self._home is not None else Path.home()

    @property
    def session_dir(self) -> Path:
        return self._resolved_home / ".claude" / "session-env" / self._session_id

    @property
    def cache_dir(self) -> Path:
        """Base cache directory for claude-hooks (auto-created)."""
        if self._xdg_cache_home is not None:
            d = self._xdg_cache_home / "claude-hooks"
            d.mkdir(parents=True, exist_ok=True)
            return d
        return Path(user_cache_dir(appname="claude-hooks", ensure_exists=True))

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
        """Upstream proxy credentials file."""
        return self.auth_proxy_dir / "upstream_proxy"

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
    def log_file(self) -> Path:
        """Session-start log file."""
        return self.session_dir / "session-start.log"

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
        return Path("/tmp/claude") / self._session_id

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
        return Path(f"/tmp/claude-hd-{self._session_id}") / "d.sock"

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
