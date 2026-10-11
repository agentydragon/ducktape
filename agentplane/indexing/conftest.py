"""Isolated PostgreSQL/pgvector databases and throwaway upstream repositories for index service tests."""

from collections.abc import AsyncGenerator, Collection, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pygit2
import pytest
import pytest_asyncio
from pygit2.enums import FileMode
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from agentplane.indexing.store import Store
from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET
from haku.recall_index.fake_embedder import FakeEmbedder
from third_party.containers import pgvector_pg18
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate
from util.testing.postgres_fixtures import database_clones, database_names, postgres_admin_url, start_postgres_container
from util.testing.sqlalchemy_engines import managed_async_engine

# gazelle:include_dep @pypi//asyncpg
# The template admin URL uses the psycopg dialect, which SQLAlchemy loads at runtime.
# gazelle:include_dep @pypi//psycopg


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    container = start_postgres_container(pgvector_pg18.IMAGE)
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def empty_index_database_template(postgres_admin_url: str) -> Generator[PostgresDatabaseTemplate]:
    """Install pgvector once; custom initialization tests clone this empty database."""
    with PostgresDatabaseTemplate(
        postgres_admin_url, "agentplane_index_empty_template", extensions=("vector",)
    ) as template:
        yield template


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def initialized_index_database_template(
    postgres_admin_url: str, empty_index_database_template: PostgresDatabaseTemplate
) -> AsyncGenerator[PostgresDatabaseTemplate]:
    """Initialize the default index schema and state once for the ordinary test cases."""
    with PostgresDatabaseTemplate(
        postgres_admin_url,
        "agentplane_index_initialized_template",
        template=empty_index_database_template.template_name,
    ) as template:
        async with managed_async_engine(
            create_async_engine(make_url(template.url).set(drivername="postgresql+asyncpg"))
        ) as opened:
            await Store(opened, budget=DEFAULT_CHUNK_BUDGET, model_key=FakeEmbedder().model_key).initialize()
        yield template


@pytest.fixture
async def engine(
    initialized_index_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones
) -> AsyncGenerator[AsyncEngine]:
    """A fresh writable clone of the default initialized index schema and state."""
    db_url = database_clones.clone(initialized_index_database_template, drivername="postgresql+asyncpg")
    async with managed_async_engine(create_async_engine(db_url)) as opened:
        yield opened


@pytest.fixture
async def empty_engine(
    empty_index_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones
) -> AsyncGenerator[AsyncEngine]:
    """A fresh pgvector database for tests that choose a non-default index configuration."""
    db_url = database_clones.clone(empty_index_database_template, drivername="postgresql+asyncpg")
    async with managed_async_engine(create_async_engine(db_url)) as opened:
        yield opened


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def store(engine: AsyncEngine, embedder: FakeEmbedder) -> Store:
    return Store(engine, budget=DEFAULT_CHUNK_BUDGET, model_key=embedder.model_key)


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
        executable: Collection[str] = (),
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
