"""Reactive state store using reaktiv signals.

Decentralized reactive architecture:
- Each source (GitstatusdListener) owns its own Signal
- Store maintains a registry of these source-owned signals
- Aggregation happens via Computed that reads from registered signals
- No callbacks needed - reaktiv dependency tracking handles propagation

Store Key Types:
    gitstatusd: Path -> Signal[Collector[GitstatusdData]] (registered by GitstatusdListener)
    worktree_paths: frozenset[Path] (set of discovered worktree paths, owned by store)
    active_branches: frozenset[str] (derived from gitstatusd branch, used by GitHubWatcher)

PR data is managed by a single GitHubWatcher that:
- Input: active_branches signal (derived from gitstatusd)
- Output: Signal[Collector[dict[str, PRData | None]]] batch keyed by branch
The join from (worktree → branch) + (branch → PR) happens at query time in status_handler.
"""

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import cast

from reaktiv import Computed, Signal

from wt.shared.protocol import Collector, GitstatusdConfig, GitstatusdData, GitstatusdUnavailable


class DaemonStore:
    """Central store for all daemon state using reaktiv signals.

    Uses a registry pattern where sources own their signals and register them
    with the store. The store aggregates via Computed.
    """

    def __init__(self) -> None:
        # Registry of source-owned signals
        # Sources register/unregister their signals; store reads them via Computed
        self._gitstatusd_sources: Signal[dict[Path, Signal[Collector[GitstatusdData]]]] = Signal({})

        # Discovered worktree paths (owned by store, updated by watcher)
        self.worktree_paths: Signal[frozenset[Path]] = Signal(frozenset())

        # Daemon config
        self._gitstatusd_config: Signal[GitstatusdConfig] = Signal(GitstatusdUnavailable(error="not initialized"))
        self._github_enabled: Signal[bool] = Signal(True)

        # Computed: all branches currently checked out across worktrees
        # Reads branch from GitstatusdData
        def _compute_active_branches() -> frozenset[str]:
            branches: set[str] = set()
            for sig in self._gitstatusd_sources().values():
                collector = sig()
                if collector.last_ok and collector.last_ok.value.branch:
                    branches.add(collector.last_ok.value.branch)
            return frozenset(branches)

        self.active_branches: Callable[[], frozenset[str]] = Computed(_compute_active_branches)

        # Computed aggregator for gitstatusd data
        def _compute_gitstatusd() -> dict[Path, Collector[GitstatusdData]]:
            return {path: sig() for path, sig in self._gitstatusd_sources().items()}

        self._gitstatusd_computed: Callable[[], dict[Path, Collector[GitstatusdData]]] = Computed(_compute_gitstatusd)

    # Registration methods - sources call these to join/leave the aggregation

    def register_gitstatusd(self, path: Path, signal: Signal[Collector[GitstatusdData]]) -> None:
        """Register a gitstatusd signal for a worktree path."""
        self._gitstatusd_sources.update(lambda d: {**d, path: signal})

    def unregister_gitstatusd(self, path: Path) -> None:
        """Unregister a gitstatusd signal for a worktree path."""
        self._gitstatusd_sources.update(lambda d: {k: v for k, v in d.items() if k != path})

    # Computed aggregators - read from all registered signals

    def gitstatusd(self) -> dict[Path, Collector[GitstatusdData]]:
        """Aggregate gitstatusd data by reading all registered signals."""
        return self._gitstatusd_computed()

    # Config accessors

    def gitstatusd_config(self) -> GitstatusdConfig:
        """Get current gitstatusd configuration."""
        return cast(GitstatusdConfig, self._gitstatusd_config())

    def set_gitstatusd_config(self, config: GitstatusdConfig) -> None:
        """Set gitstatusd configuration."""
        self._gitstatusd_config.set(config)

    def github_enabled(self) -> bool:
        """Get whether GitHub is enabled."""
        return cast(bool, self._github_enabled())

    def set_github_enabled(self, enabled: bool) -> None:
        """Set whether GitHub is enabled."""
        self._github_enabled.set(enabled)

    # Worktree path management (owned by store)

    def add_worktree_path(self, path: Path) -> None:
        """Add a worktree path to the set."""
        self.worktree_paths.update(lambda s: s | {path})

    def remove_worktree_path(self, path: Path) -> None:
        """Remove a worktree path from the set."""
        self.worktree_paths.update(lambda s: s - {path})

    def set_worktree_paths(self, paths: Iterable[Path]) -> None:
        """Set all worktree paths at once."""
        self.worktree_paths.set(frozenset(paths))
