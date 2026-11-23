"""Build a sorted display tree with per-directory change totals."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from difftree.config import SortMode
from difftree.parser import FileChange


@dataclass(frozen=True)
class TreeNode:
    name: str
    is_file: bool
    additions: int
    deletions: int
    is_binary: bool = False
    children: tuple[TreeNode, ...] = ()

    @property
    def total_changes(self) -> int:
        return self.additions + self.deletions


@dataclass
class _PathNode:
    # A patch can delete a file and add children at the same path, or vice versa.
    change: FileChange | None = None
    children: dict[str, _PathNode] = field(default_factory=dict)

    def directory(self, name: str, sort_by: SortMode) -> TreeNode:
        children = []
        for child_name, child in self.children.items():
            if (change := child.change) is not None:
                children.append(
                    TreeNode(
                        child_name,
                        is_file=True,
                        additions=change.additions,
                        deletions=change.deletions,
                        is_binary=change.is_binary,
                    )
                )
            if child.children:
                children.append(child.directory(child_name, sort_by))

        if sort_by == SortMode.SIZE:
            children.sort(key=lambda child: child.total_changes, reverse=True)
        else:
            children.sort(key=lambda child: child.name)
        return TreeNode(
            name,
            is_file=False,
            additions=sum(child.additions for child in children),
            deletions=sum(child.deletions for child in children),
            children=tuple(children),
        )


def build_tree(changes: list[FileChange], sort_by: SortMode = SortMode.SIZE) -> TreeNode:
    root = _PathNode()
    for change in changes:
        current = root
        for part in Path(change.path).parts:
            current = current.children.setdefault(part, _PathNode())
        current.change = change

    return root.directory(Path.cwd().name or ".", sort_by)
