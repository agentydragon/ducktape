"""Isolated PostgreSQL/pgvector databases and throwaway upstream repositories for index service tests."""

from collections.abc import AsyncGenerator, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pygit2
import pytest
from pygit2.enums import FileMode
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET
from haku.recall_index.fake_embedder import FakeEmbedder
from third_party.containers.rlocations import PGVECTOR_PG18
from util.testing.postgres_fixtures import start_postgres_container
from x.agentplane.indexing.store import Store

# gazelle:include_dep @pypi//asyncpg


@pytest.fixture(scope="session")
def pgvector_container() -> Generator[PostgresContainer]:
    container = start_postgres_container(PGVECTOR_PG18)
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
async def engine(pgvector_container: PostgresContainer) -> AsyncGenerator[AsyncEngine]:
    host = pgvector_container.get_container_host_ip()
    port = int(pgvector_container.get_exposed_port(5432))
    opened = create_async_engine(f"postgresql+asyncpg://postgres:postgres@{host}:{port}/postgres")
    try:
        async with opened.begin() as connection:
            await connection.exec_driver_sql("DROP SCHEMA IF EXISTS agentplane_index CASCADE")
        yield opened
    finally:
        await opened.dispose()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
async def store(engine: AsyncEngine, embedder: FakeEmbedder) -> Store:
    result = Store(engine, budget=DEFAULT_CHUNK_BUDGET, model_key=embedder.model_key)
    await result.initialize()
    return result


@dataclass
class Upstream:
    """A non-bare repository the source under test fetches from, driven by commits."""

    repository: pygit2.Repository

    @property
    def url(self) -> str:
        return self.repository.workdir

    def commit(
        self,
        files: Mapping[str, bytes | None],
        *,
        executable: frozenset[str] = frozenset(),
        symlinks: Mapping[str, str] | None = None,
        submodules: Mapping[str, str] | None = None,
    ) -> str:
        """Write `files` (None deletes), add them, and commit on the current branch; returns the commit id."""
        workdir = Path(self.repository.workdir)
        index = self.repository.index
        for path, data in files.items():
            target = workdir / path
            if data is None:
                target.unlink()
                index.remove(path)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if path in executable:
                target.chmod(0o755)
            index.add(path)
        for path, destination in (symlinks or {}).items():
            (workdir / path).symlink_to(destination)
            index.add(path)
        for path, commit_id in (submodules or {}).items():
            index.add(pygit2.IndexEntry(path, pygit2.Oid(hex=commit_id), FileMode.COMMIT))
        index.write()
        signature = pygit2.Signature("Index Test", "index@test.invalid")
        parents = [] if self.repository.head_is_unborn else [self.repository.head.target]
        return str(self.repository.create_commit("HEAD", signature, signature, "test", index.write_tree(), parents))

    def tree_id(self, revision: str) -> str:
        return str(self.repository[revision].peel(pygit2.Commit).tree_id)


@pytest.fixture
def upstream(tmp_path: Path) -> Upstream:
    return Upstream(pygit2.init_repository(str(tmp_path / "upstream"), initial_head="main"))
