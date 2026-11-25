"""Test registry for the image planner and publisher, at Crane's command boundary."""

from pathlib import Path

from devinfra.oci.crane import Crane, CraneError


class FakeCrane(Crane):
    def __init__(self, repos: dict[str, dict[str, str]], *, errors: dict[str, str] | None = None) -> None:
        super().__init__(Path("/nonexistent/crane"))
        self.repos = repos
        self.errors = errors or {}
        self.pushed: list[tuple[Path, str]] = []
        self.tagged: list[tuple[str, str]] = []

    def _run(self, *args: str) -> str:
        if error := self.errors.get(args[0]):
            raise CraneError(args, 1, error, "")
        match args:
            case ("ls", repo):
                if repo not in self.repos:
                    raise CraneError(args, 1, "NAME_UNKNOWN: unknown package", "")
                return "\n".join(self.repos[repo])
            case ("digest", ref):
                repo, _, tag = ref.rpartition(":")
                if (digest := self.repos.get(repo, {}).get(tag)) is None:
                    raise CraneError(args, 1, "MANIFEST_UNKNOWN: unknown manifest", "")
                return digest
            case ("push", directory, ref):
                self.pushed.append((Path(directory), ref))
            case ("tag", ref, tag):
                self.tagged.append((ref, tag))
            case _:
                raise AssertionError(f"unexpected crane command: {args}")
        return ""
