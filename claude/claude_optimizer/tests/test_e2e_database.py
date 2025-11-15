"""End-to-end test for database integration with mocked APIs."""

from datetime import datetime
import json
from pathlib import Path
from unittest.mock import Mock

from claude_optimizer.core.yaml_loader import YamlLoader
from claude_optimizer.database.models import (
    GradingCriteria,
    OptimizationRun,
    Rollout,
    RolloutFile,
    SeedTask,
    SystemPrompt,
    create_database,
)
import pytest
import yaml


@pytest.fixture
def temp_db():
    """Create a temporary in-memory database for testing."""
    session_local = create_database("sqlite:///:memory:")
    session = session_local()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def sample_seeds_yaml(tmp_path):
    """Create a sample seeds.yaml file with required fields."""
    seeds_data = [
        {
            "id": "test_task_001",
            "prompt": "Create a simple REST API client that makes HTTP requests.",
            "description": "Basic HTTP client test",
            "docker_image": "claude-dev:python",
            "allowed_tools": ["Read", "Write", "Edit"],
        },
        {
            "id": "test_task_002",
            "prompt": "Build a configuration loader that reads from multiple sources.",
            "description": "Configuration management test",
            "docker_image": "claude-dev:python",
            "allowed_tools": ["Read", "Write", "Edit"],
        },
    ]

    seeds_file = tmp_path / "seeds.yaml"
    with seeds_file.open("w") as f:
        yaml.dump(seeds_data, f)

    return seeds_file


@pytest.fixture
def sample_graders_yaml(tmp_path):
    """Create a sample graders.yaml file."""
    graders_data = {
        "graders": [
            {
                "name": "type_safety_test",
                "description": "Test type safety and data design",
                "evaluation_criteria": "Check for proper type annotations and data structures.",
            },
            {
                "name": "code_quality_test",
                "description": "Test code quality and clarity",
                "evaluation_criteria": "Evaluate code readability and modern practices.",
            },
        ]
    }

    graders_file = tmp_path / "graders.yaml"
    with graders_file.open("w") as f:
        yaml.dump(graders_data, f)

    return graders_file


class TestYamlDatabaseSync:
    """Test YAML file synchronization with database."""

    def test_sync_seed_tasks(self, temp_db, sample_seeds_yaml):
        """Test syncing seed tasks from YAML to database."""
        yaml_loader = YamlLoader(sample_seeds_yaml, Path("nonexistent.yaml"))

        session = temp_db
        stats = yaml_loader.sync_seed_tasks(session)
        session.commit()

        assert stats["seeds_added"] == 2
        assert stats["seeds_updated"] == 0

        tasks = session.query(SeedTask).all()
        assert len(tasks) == 2

        task_ids = [t.task_id for t in tasks]
        assert "test_task_001" in task_ids
        assert "test_task_002" in task_ids

        task_001 = session.query(SeedTask).filter_by(task_id="test_task_001").first()
        assert "REST API client" in task_001.prompt
        assert task_001.description == "Basic HTTP client test"
        assert task_001.is_active is True

    def test_sync_grading_criteria(self, temp_db, sample_graders_yaml):
        """Test syncing grading criteria from YAML to database."""
        yaml_loader = YamlLoader(Path("nonexistent.yaml"), sample_graders_yaml)

        session = temp_db
        stats = yaml_loader.sync_grading_criteria(session)
        session.commit()

        assert stats["graders_added"] == 2
        assert stats["graders_updated"] == 0

        criteria = session.query(GradingCriteria).all()
        assert len(criteria) == 2

        names = [c.name for c in criteria]
        assert "type_safety_test" in names
        assert "code_quality_test" in names

        type_safety = session.query(GradingCriteria).filter_by(name="type_safety_test").first()
        assert "type annotations" in type_safety.evaluation_criteria
        assert type_safety.is_active is True

    def test_content_hash_change_detection(self, temp_db, sample_seeds_yaml):
        """Test that content hash changes are properly detected."""
        yaml_loader = YamlLoader(sample_seeds_yaml, Path("nonexistent.yaml"))

        session = temp_db
        yaml_loader.sync_seed_tasks(session)
        session.commit()

        task = session.query(SeedTask).filter_by(task_id="test_task_001").first()
        original_hash = task.content_hash

        with sample_seeds_yaml.open() as f:
            data = yaml.safe_load(f)
        data[0]["prompt"] = "MODIFIED: Create a different REST API client."
        with sample_seeds_yaml.open("w") as f:
            yaml.dump(data, f)

        stats = yaml_loader.sync_seed_tasks(session)
        session.commit()

        assert stats["seeds_added"] == 0
        assert stats["seeds_updated"] == 1

        task = session.query(SeedTask).filter_by(task_id="test_task_001").first()
        assert task.content_hash != original_hash
        assert "MODIFIED:" in task.prompt


class TestDatabaseModels:
    """Test database models and relationships."""

    def test_optimization_run_creation(self, temp_db):
        """Test creating an optimization run record."""
        with temp_db.get_session() as session:
            run = OptimizationRun(
                start_time=datetime.utcnow(),
                base_output_dir="/tmp/test_run",
                total_iterations=3,
                config_snapshot='{"test": true}',
                status="running",
            )
            session.add(run)
            session.commit()

            # Verify it was created
            retrieved_run = session.query(OptimizationRun).first()
            assert retrieved_run.base_output_dir == "/tmp/test_run"
            assert retrieved_run.total_iterations == 3
            assert retrieved_run.status == "running"

    def test_system_prompt_with_content_hash(self, temp_db):
        """Test system prompt storage with content hashing."""
        with temp_db.get_session() as session:
            # Create run first
            run = OptimizationRun(
                start_time=datetime.utcnow(), base_output_dir="/tmp/test", total_iterations=1, status="running"
            )
            session.add(run)
            session.flush()  # Get the ID

            # Create system prompt
            content = "# CLAUDE.md\nThis is a test system prompt."
            prompt = SystemPrompt(
                run_id=run.id, iteration=0, content=content, content_hash=SystemPrompt.compute_content_hash(content)
            )
            session.add(prompt)
            session.commit()

            # Verify storage
            retrieved_prompt = session.query(SystemPrompt).first()
            assert retrieved_prompt.content == content
            assert retrieved_prompt.iteration == 0
            assert len(retrieved_prompt.content_hash) == 64  # SHA256 hex length

    def test_rollout_file_integrity_checking(self, temp_db, tmp_path):
        """Test file integrity checking with SHA256 hashes."""
        # Create a test file
        test_file = tmp_path / "test_output.py"
        test_content = "print('Hello, world!')\n"
        test_file.write_text(test_content)

        session = temp_db
        run = OptimizationRun(start_time=datetime.utcnow(), base_output_dir="/tmp", status="running")
        session.add(run)
        session.flush()

        task = SeedTask(
            task_id="test_task",
            prompt="Test prompt",
            description=None,
            allowed_tools=["Read", "Write"],
            docker_image="claude-dev:python",
            pre_task_commands=None,
            content_hash="dummy_hash",
        )
        session.add(task)
        session.flush()

        prompt = SystemPrompt(run_id=run.id, iteration=0, content="Test content", content_hash="dummy_hash")
        session.add(prompt)
        session.flush()

        rollout = Rollout(
            run_id=run.id,
            iteration=0,
            task_id=task.id,
            agent_id="test_agent",
            system_prompt_id=prompt.id,
            output_dir_path="/tmp/test",
        )
        session.add(rollout)
        session.flush()

        rollout_file = RolloutFile(
            rollout_id=rollout.id,
            relative_path="test_output.py",
            absolute_path=str(test_file),
            content_sha256=RolloutFile.compute_file_hash(test_file),
            file_size=len(test_content),
        )
        session.add(rollout_file)
        session.commit()

        assert rollout_file.verify_file_integrity() is True

        read_content = rollout_file.read_content()
        assert read_content == test_content

        test_file.write_text("MODIFIED CONTENT")
        assert rollout_file.verify_file_integrity() is False


class TestEndToEndWorkflow:
    """Test end-to-end workflow with mocked APIs."""

    @pytest.fixture
    def mock_openai_response(self):
        """Mock OpenAI response for grading."""
        response = Mock()
        response.output = [
            Mock(
                type="function_call",
                function_call=Mock(
                    arguments=json.dumps(
                        {
                            "overall_score": 7.5,
                            "overall_rationale": "Good implementation with minor issues.",
                            "facet_scores": {
                                "type_safety_test": {"score": 8.0, "rationale": "Good type annotations."},
                                "code_quality_test": {
                                    "score": 7.0,
                                    "rationale": "Code is readable but could be more concise.",
                                },
                            },
                        }
                    )
                ),
            )
        ]
        return response

    @pytest.fixture
    def mock_claude_sdk(self):
        """Mock Claude SDK responses."""
        return [
            Mock(role="user", content="Create a REST API client"),
            Mock(role="assistant", content="I'll create an HTTP client for you..."),
            Mock(role="tool_result", content="Files created successfully"),
        ]

    def test_database_integration_workflow(self, temp_db, sample_seeds_yaml, sample_graders_yaml, tmp_path):
        """Test that the basic database workflow works."""
        # Load YAML files
        yaml_loader = YamlLoader(sample_seeds_yaml, sample_graders_yaml)
        with temp_db.get_session() as session:
            stats = yaml_loader.load_and_sync_all(session)

        assert stats["seeds_added"] == 2
        assert stats["graders_added"] == 2

        # Create optimization run
        with temp_db.get_session() as session:
            run = OptimizationRun(
                start_time=datetime.utcnow(), base_output_dir=str(tmp_path), total_iterations=1, status="running"
            )
            session.add(run)
            session.commit()
            run_id = run.id

        # Verify we can query everything properly
        with temp_db.get_session() as session:
            # Check run exists
            run = session.query(OptimizationRun).filter_by(id=run_id).first()
            assert run is not None

            # Check tasks were loaded
            tasks = session.query(SeedTask).filter_by(is_active=True).all()
            assert len(tasks) == 2

            # Check grading criteria were loaded
            criteria = session.query(GradingCriteria).filter_by(is_active=True).all()
            assert len(criteria) == 2

            # Verify full text content is searchable
            rest_tasks = session.query(SeedTask).filter(SeedTask.prompt.like("%REST API%")).all()
            assert len(rest_tasks) == 1

            type_criteria = (
                session.query(GradingCriteria)
                .filter(GradingCriteria.evaluation_criteria.like("%type annotations%"))
                .all()
            )
            assert len(type_criteria) == 1
