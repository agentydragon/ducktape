"""Full end-to-end test with mocked OpenAI and Claude SDK APIs."""

from datetime import datetime
import json
from unittest.mock import Mock

from claude_optimizer.core.yaml_loader import YamlLoader
from claude_optimizer.database.models import (
    GraderFacetResult,
    GraderRun,
    GradingCriteria,
    OptimizationRun,
    PatternAnalysis,
    PatternAnalysisRollout,
    Rollout,
    RolloutFile,
    RolloutMessage,
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
    """Create a sample seeds.yaml file with realistic tasks and required fields."""
    seeds_data = [
        {
            "id": "test_rest_api",
            "prompt": "Create a REST API client that calls backends A and B in parallel, then sends their combined responses to C. Return a reasonable response to client event if backends fail.\n\nWrite working Python code to files. Include a main module and any needed helper files.",
            "description": "Test REST API implementation",
            "docker_image": "claude-dev:python",
            "allowed_tools": ["Read", "Write", "Edit"],
        },
        {
            "id": "test_config_loader",
            "prompt": "Build a configuration loader that reads settings from multiple sources (files, environment variables, command line). If cli doesn't specify config, read from env var, then from file. Config is: backend URL, listening port & host for our app, log level.\n\nWrite working Python code to files. Include a main module and any needed helper files.",
            "description": "Test configuration management",
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
    """Create a sample graders.yaml file with realistic criteria."""
    graders_data = {
        "graders": [
            {
                "name": "type_safety_data_design",
                "description": "Use the type system to make invalid states unrepresentable",
                "evaluation_criteria": """Type annotations everywhere except experimental scripts.
Use specific types that express intent and constraints.
Enums for fixed choices (never string literals like "active"/"inactive").""",
            },
            {
                "name": "code_quality_clarity",
                "description": "Code should be readable, modern, and refined",
                "evaluation_criteria": """Write code for stressed humans with limited working memory.
Use idiomatic features of your language version.
Early returns and guard clauses (max 2-3 nesting levels).""",
            },
            {
                "name": "robustness_error_handling",
                "description": "Fail fast and loudly on unexpected conditions",
                "evaluation_criteria": """Catch only handleable exceptions.
Let programming errors crash (expose bugs).
Programs must signal failure appropriately.""",
            },
        ]
    }

    graders_file = tmp_path / "graders.yaml"
    with graders_file.open("w") as f:
        yaml.dump(graders_data, f)

    return graders_file


class MockCloseableSession:
    """Mock session context manager that can be closed."""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class TestFullEndToEndWorkflow:
    """Test complete optimization workflow with mocked APIs."""

    @pytest.fixture
    def mock_openai_grading_response(self):
        """Mock OpenAI response for grading with realistic scores."""

        def create_response(overall_score=7.2, type_score=8.0, quality_score=6.8, robust_score=6.9):
            response = Mock()
            response.output = [
                Mock(
                    type="function_call",
                    function_call=Mock(
                        arguments=json.dumps(
                            {
                                "overall_score": overall_score,
                                "overall_rationale": f"Implementation shows good understanding with score {overall_score}. Well-structured code with proper error handling, though could be more concise in some areas.",
                                "facet_scores": {
                                    "type_safety_data_design": {
                                        "score": type_score,
                                        "rationale": f"Good type annotations and data structures (score: {type_score}). Uses proper type hints and avoids string literals for enums.",
                                    },
                                    "code_quality_clarity": {
                                        "score": quality_score,
                                        "rationale": f"Code is readable but has some complexity (score: {quality_score}). Could benefit from more guard clauses and simpler control flow.",
                                    },
                                    "robustness_error_handling": {
                                        "score": robust_score,
                                        "rationale": f"Decent error handling but catches some broad exceptions (score: {robust_score}). Should be more specific about exception types.",
                                    },
                                },
                            }
                        )
                    ),
                )
            ]
            return response

        return create_response

    @pytest.fixture
    def mock_openai_prompt_engineering_response(self):
        """Mock OpenAI response for prompt engineering with realistic improvements."""
        response = Mock()
        response.output = [
            Mock(
                type="function_call",
                function_call=Mock(
                    arguments=json.dumps(
                        {
                            "updated_prompt": """# CLAUDE.md

## Code Quality Standards

Write production-ready code that follows these principles:

### Type Safety
- Use specific type annotations everywhere
- Prefer enums over string literals for choices
- Use Path objects instead of strings for file paths

### Error Handling  
- Catch only specific, expected exceptions
- Let programming errors crash with clear messages
- Use early returns and guard clauses

### Code Clarity
- Write for human readers with limited working memory
- Use modern language features and idioms
- Keep functions short and focused

### Architecture
- Single responsibility principle
- Dependency injection over global state
- Use appropriate libraries instead of manual parsing

Always implement exactly what is requested - no more, no less.""",
                            "reasoning": "Based on the pattern analysis, the main issues were overly broad exception handling and some string literals used instead of proper types. The updated prompt emphasizes specific exception handling and type safety to address these patterns.",
                        }
                    )
                ),
            )
        ]
        return response

    @pytest.fixture
    def mock_claude_sdk_messages(self):
        """Mock Claude SDK conversation messages."""

        def create_messages(task_id="test_rest_api"):
            if "rest_api" in task_id:
                return [
                    {"role": "user", "content": "Create a REST API client that calls backends A and B in parallel..."},
                    {
                        "role": "assistant",
                        "content": "I'll create a REST API client that calls multiple backends in parallel. Let me start by creating the main client module.",
                        "tool_uses": [
                            {
                                "name": "Write",
                                "input": {
                                    "file_path": "/tmp/api_client.py",
                                    "content": """import asyncio
import aiohttp
from typing import Dict, Any, Optional
from enum import Enum

class BackendStatus(Enum):
    SUCCESS = \"success\"
    FAILED = \"failed\"

class APIClient:
    def __init__(self, backend_a_url: str, backend_b_url: str, backend_c_url: str):
        self.backend_a_url = backend_a_url
        self.backend_b_url = backend_b_url  
        self.backend_c_url = backend_c_url
    
    async def call_backends_parallel(self) -> Dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            # Call A and B in parallel
            tasks = [
                self._call_backend(session, self.backend_a_url, \"A\"),
                self._call_backend(session, self.backend_b_url, \"B\")
            ]
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Combine responses
            combined_response = {
                \"backend_a\": results[0] if not isinstance(results[0], Exception) else None,
                \"backend_b\": results[1] if not isinstance(results[1], Exception) else None,
                \"status\": BackendStatus.SUCCESS if all(not isinstance(r, Exception) for r in results) else BackendStatus.FAILED
            }
            
            # Send to backend C
            return await self._send_to_backend_c(session, combined_response)
    
    async def _call_backend(self, session: aiohttp.ClientSession, url: str, backend_name: str) -> Dict[str, Any]:
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            raise RuntimeError(f\"Backend {backend_name} failed: {e}\")
    
    async def _send_to_backend_c(self, session: aiohttp.ClientSession, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            async with session.post(self.backend_c_url, json=data) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            # Return reasonable fallback response
            return {\"status\": \"partial_success\", \"error\": str(e), \"data\": data}
""",
                                },
                            }
                        ],
                    },
                    {"role": "tool_result", "content": "File created successfully at /tmp/api_client.py"},
                    {
                        "role": "assistant",
                        "content": "Now let me create a main module to demonstrate usage:",
                        "tool_uses": [
                            {
                                "name": "Write",
                                "input": {
                                    "file_path": "/tmp/main.py",
                                    "content": """#!/usr/bin/env python3
import asyncio
from api_client import APIClient

async def main():
    client = APIClient(
        backend_a_url=\"http://api-a.example.com/data\",
        backend_b_url=\"http://api-b.example.com/data\", 
        backend_c_url=\"http://api-c.example.com/combine\"
    )
    
    try:
        result = await client.call_backends_parallel()
        print(f\"Success: {result}\")
    except Exception as e:
        print(f\"Failed: {e}\")
        return 1
    
    return 0

if __name__ == \"__main__\":
    exit_code = asyncio.run(main())
    exit(exit_code)
""",
                                },
                            }
                        ],
                    },
                    {"role": "tool_result", "content": "File created successfully at /tmp/main.py"},
                ]
            # Config loader task
            return [
                {
                    "role": "user",
                    "content": "Build a configuration loader that reads settings from multiple sources...",
                },
                {
                    "role": "assistant",
                    "content": "I'll create a configuration loader that reads from files, environment variables, and command line arguments in the correct precedence order.",
                    "tool_uses": [
                        {
                            "name": "Write",
                            "input": {
                                "file_path": "/tmp/config_loader.py",
                                "content": """from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import os
import json
import argparse
from enum import Enum

class LogLevel(Enum):
    DEBUG = \"debug\"
    INFO = \"info\" 
    WARNING = \"warning\"
    ERROR = \"error\"

@dataclass
class AppConfig:
    backend_url: str
    listening_port: int
    listening_host: str
    log_level: LogLevel

class ConfigLoader:
    def load_config(self) -> AppConfig:
        # Start with defaults
        config = {
            \"backend_url\": \"http://localhost:8000\",
            \"listening_port\": 3000,
            \"listening_host\": \"0.0.0.0\", 
            \"log_level\": LogLevel.INFO.value
        }
        
        # Override with file config
        file_config = self._load_from_file()
        if file_config:
            config.update(file_config)
        
        # Override with environment variables
        env_config = self._load_from_env()
        config.update(env_config)
        
        # Override with command line arguments
        cli_config = self._load_from_cli()
        config.update(cli_config)
        
        return AppConfig(
            backend_url=config[\"backend_url\"],
            listening_port=config[\"listening_port\"],
            listening_host=config[\"listening_host\"],
            log_level=LogLevel(config[\"log_level\"])
        )
    
    def _load_from_file(self) -> Optional[dict]:
        config_path = Path(\"config.json\")
        if config_path.exists():
            try:
                with open(config_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                raise RuntimeError(f\"Failed to load config file: {e}\")
        return None
    
    def _load_from_env(self) -> dict:
        env_config = {}
        if url := os.getenv(\"BACKEND_URL\"):
            env_config[\"backend_url\"] = url
        if port := os.getenv(\"LISTENING_PORT\"):
            env_config[\"listening_port\"] = int(port)
        if host := os.getenv(\"LISTENING_HOST\"):
            env_config[\"listening_host\"] = host
        if level := os.getenv(\"LOG_LEVEL\"):
            env_config[\"log_level\"] = level
        return env_config
    
    def _load_from_cli(self) -> dict:
        parser = argparse.ArgumentParser()
        parser.add_argument(\"--backend-url\", help=\"Backend URL\")
        parser.add_argument(\"--listening-port\", type=int, help=\"Listening port\")
        parser.add_argument(\"--listening-host\", help=\"Listening host\")
        parser.add_argument(\"--log-level\", choices=[l.value for l in LogLevel], help=\"Log level\")
        
        args = parser.parse_args()
        
        cli_config = {}
        if args.backend_url:
            cli_config[\"backend_url\"] = args.backend_url
        if args.listening_port:
            cli_config[\"listening_port\"] = args.listening_port
        if args.listening_host:
            cli_config[\"listening_host\"] = args.listening_host
        if args.log_level:
            cli_config[\"log_level\"] = args.log_level
            
        return cli_config
""",
                            },
                        }
                    ],
                },
                {"role": "tool_result", "content": "File created successfully at /tmp/config_loader.py"},
            ]

        return create_messages

    def test_full_optimization_workflow(
        self,
        temp_db,
        sample_seeds_yaml,
        sample_graders_yaml,
        tmp_path,
        mock_openai_grading_response,
        mock_openai_prompt_engineering_response,
        mock_claude_sdk_messages,
    ):
        """Test complete 2-iteration optimization workflow with database integration."""

        # Setup YAML files and database
        yaml_loader = YamlLoader(sample_seeds_yaml, sample_graders_yaml)
        with temp_db.get_session() as session:
            sync_stats = yaml_loader.load_and_sync_all(session)

        # Verify initial data load
        assert sync_stats["seeds_added"] == 2
        assert sync_stats["graders_added"] == 3

        # Create optimization run
        with temp_db.get_session() as session:
            run = OptimizationRun(
                start_time=datetime.utcnow(),
                base_output_dir=str(tmp_path),
                total_iterations=2,
                config_snapshot='{"max_parallel_rollouts": 2}',
                status="running",
            )
            session.add(run)
            session.commit()
            run_id = run.id

        # Create initial system prompt
        initial_prompt_content = """# CLAUDE.md

Write clean, well-documented Python code that follows best practices.
Use proper error handling and type annotations.
"""

        with temp_db.get_session() as session:
            initial_prompt = SystemPrompt(
                run_id=run_id,
                iteration=0,
                content=initial_prompt_content,
                content_hash=SystemPrompt.compute_content_hash(initial_prompt_content),
            )
            session.add(initial_prompt)
            session.commit()
            initial_prompt_id = initial_prompt.id

        # ITERATION 0: Initial rollouts
        with temp_db.get_session() as session:
            tasks = session.query(SeedTask).filter_by(is_active=True).all()
            criteria = session.query(GradingCriteria).filter_by(is_active=True).all()

            rollout_data = []

            # Create rollouts for each task
            for i, task in enumerate(tasks[:2]):  # Test with 2 tasks
                # Create rollout record
                rollout = Rollout(
                    run_id=run_id,
                    iteration=0,
                    task_id=task.id,
                    agent_id=f"agent_{i + 1}",
                    system_prompt_id=initial_prompt_id,
                    start_time=datetime.utcnow(),
                    end_time=datetime.utcnow(),
                    total_cost_usd=0.15,
                    is_error=False,
                    duration_ms=45000,
                    output_dir_path=str(tmp_path / f"rollout_{i + 1}"),
                )
                session.add(rollout)
                session.flush()  # Get the ID

                # Create mock conversation messages
                messages = mock_claude_sdk_messages(task.task_id)
                for j, msg in enumerate(messages):
                    rollout_msg = RolloutMessage(
                        rollout_id=rollout.id,
                        sequence_order=j,
                        message_type=msg["role"],
                        content=json.dumps(msg),
                        timestamp=datetime.utcnow(),
                    )
                    session.add(rollout_msg)

                # Create mock output files
                output_files = (
                    [("api_client.py", "# API client code"), ("main.py", "# Main module")]
                    if "rest" in task.task_id
                    else [("config_loader.py", "# Config loader code")]
                )
                for filename, content in output_files:
                    file_path = tmp_path / f"rollout_{i + 1}" / filename
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(content)

                    rollout_file = RolloutFile(
                        rollout_id=rollout.id,
                        relative_path=filename,
                        absolute_path=str(file_path),
                        content_sha256=RolloutFile.compute_file_hash(file_path),
                        file_size=len(content),
                    )
                    session.add(rollout_file)

                # Create grading results (vary scores for realistic testing)
                base_score = 7.2 if i == 0 else 6.8
                grader_run = GraderRun(
                    rollout_id=rollout.id,
                    overall_score=base_score,
                    overall_rationale=f"Implementation shows understanding with score {base_score}. Code structure is good but could be improved.",
                    grader_model="o3",
                    grader_reasoning='{"reasoning": "analyzed patterns and quality"}',
                )
                session.add(grader_run)
                session.flush()

                # Create facet results
                facet_scores = [8.0, 6.5, 7.0] if i == 0 else [7.5, 6.2, 6.5]
                for j, criterion in enumerate(criteria):
                    facet_result = GraderFacetResult(
                        grader_run_id=grader_run.id,
                        criterion_id=criterion.id,
                        score=facet_scores[j],
                        rationale=f"Facet {criterion.name} scored {facet_scores[j]} based on implementation quality.",
                        facet_order=j,
                    )
                    session.add(facet_result)

                rollout_data.append({"rollout": rollout, "grader_run": grader_run, "files": output_files})

            session.commit()

        # Create pattern analysis for iteration 0
        with temp_db.get_session() as session:
            rollouts = session.query(Rollout).filter_by(iteration=0).all()

            pattern_analysis = PatternAnalysis(
                run_id=run_id,
                iteration=0,
                input_rollout_count=len(rollouts),
                summary_text="""Pattern Analysis Summary:

Common issues identified:
1. Some broad exception handling (catching Exception instead of specific types)
2. String literals used for status values instead of enums
3. Could benefit from more type annotations

Strengths:
- Good overall structure and modularity
- Proper async/await usage
- Reasonable error handling strategies""",
                tokens_used=1200,
                analysis_reasoning='{"patterns": ["broad_exceptions", "string_literals"], "strengths": ["structure", "async_usage"]}',
            )
            session.add(pattern_analysis)
            session.flush()

            # Link rollouts to pattern analysis
            for i, rollout in enumerate(rollouts):
                pattern_rollout = PatternAnalysisRollout(
                    pattern_analysis_id=pattern_analysis.id, rollout_id=rollout.id, rollout_order=i
                )
                session.add(pattern_rollout)

            session.commit()

        # Generate improved system prompt for iteration 1
        improved_prompt_content = mock_openai_prompt_engineering_response.output[0].function_call.arguments
        improved_prompt_data = json.loads(improved_prompt_content)

        with temp_db.get_session() as session:
            improved_prompt = SystemPrompt(
                run_id=run_id,
                iteration=1,
                content=improved_prompt_data["updated_prompt"],
                content_hash=SystemPrompt.compute_content_hash(improved_prompt_data["updated_prompt"]),
                prompt_engineer_reasoning=improved_prompt_content,
            )
            session.add(improved_prompt)
            session.commit()
            improved_prompt_id = improved_prompt.id

        # ITERATION 1: Improved rollouts (simulate better scores)
        with temp_db.get_session() as session:
            tasks = session.query(SeedTask).filter_by(is_active=True).all()
            criteria = session.query(GradingCriteria).filter_by(is_active=True).all()

            for i, task in enumerate(tasks[:2]):
                # Create improved rollout
                rollout = Rollout(
                    run_id=run_id,
                    iteration=1,
                    task_id=task.id,
                    agent_id=f"agent_{i + 1}_v2",
                    system_prompt_id=improved_prompt_id,
                    start_time=datetime.utcnow(),
                    end_time=datetime.utcnow(),
                    total_cost_usd=0.18,
                    is_error=False,
                    duration_ms=42000,
                    output_dir_path=str(tmp_path / f"rollout_iter1_{i + 1}"),
                )
                session.add(rollout)
                session.flush()

                # Better grading results (showing improvement)
                improved_score = 8.1 if i == 0 else 7.9
                grader_run = GraderRun(
                    rollout_id=rollout.id,
                    overall_score=improved_score,
                    overall_rationale=f"Significant improvement with score {improved_score}. Better type safety and error handling.",
                    grader_model="o3",
                )
                session.add(grader_run)
                session.flush()

                # Improved facet scores
                improved_facet_scores = [8.8, 7.8, 7.7] if i == 0 else [8.5, 7.5, 7.4]
                for j, criterion in enumerate(criteria):
                    facet_result = GraderFacetResult(
                        grader_run_id=grader_run.id,
                        criterion_id=criterion.id,
                        score=improved_facet_scores[j],
                        rationale=f"Much improved {criterion.name} with score {improved_facet_scores[j]}. Better implementation of best practices.",
                        facet_order=j,
                    )
                    session.add(facet_result)

            session.commit()

        # Complete the optimization run
        with temp_db.get_session() as session:
            run = session.query(OptimizationRun).filter_by(id=run_id).first()
            run.end_time = datetime.utcnow()
            run.status = "completed"
            session.commit()

        # VERIFICATION: Test that all data was stored correctly
        with temp_db.get_session() as session:
            # Check optimization run
            run = session.query(OptimizationRun).filter_by(id=run_id).first()
            assert run.status == "completed"
            assert run.total_iterations == 2

            # Check system prompts
            prompts = session.query(SystemPrompt).filter_by(run_id=run_id).all()
            assert len(prompts) == 2
            assert prompts[0].iteration == 0
            assert prompts[1].iteration == 1
            assert "Type Safety" in prompts[1].content  # Improved prompt

            # Check rollouts
            rollouts = session.query(Rollout).filter_by(run_id=run_id).all()
            assert len(rollouts) == 4  # 2 tasks x 2 iterations

            iter0_rollouts = [r for r in rollouts if r.iteration == 0]
            iter1_rollouts = [r for r in rollouts if r.iteration == 1]
            assert len(iter0_rollouts) == 2
            assert len(iter1_rollouts) == 2

            # Check grading results
            all_grades = session.query(GraderRun).all()
            assert len(all_grades) == 4

            # Check that iter1 scores are higher (showing improvement)
            iter0_scores = [gr.overall_score for gr in all_grades if gr.rollout.iteration == 0]
            iter1_scores = [gr.overall_score for gr in all_grades if gr.rollout.iteration == 1]

            assert max(iter1_scores) > max(iter0_scores)  # Best score improved
            assert sum(iter1_scores) / len(iter1_scores) > sum(iter0_scores) / len(iter0_scores)  # Average improved

            # Check facet results
            facet_results = session.query(GraderFacetResult).all()
            assert len(facet_results) == 12  # 4 rollouts x 3 criteria each

            # Check pattern analysis
            analyses = session.query(PatternAnalysis).filter_by(run_id=run_id).all()
            assert len(analyses) == 1
            assert "broad exception handling" in analyses[0].summary_text
            assert analyses[0].input_rollout_count == 2

            # Test rich content queries
            rest_tasks = session.query(SeedTask).filter(SeedTask.prompt.like("%REST API%")).all()
            assert len(rest_tasks) == 1

            type_criteria = (
                session.query(GradingCriteria)
                .filter(GradingCriteria.evaluation_criteria.like("%Type annotations%"))
                .all()
            )
            assert len(type_criteria) == 1

            # Test that we can query for score evolution
            score_evolution = (
                session.query(Rollout.iteration, GraderRun.overall_score, SeedTask.task_id)
                .join(GraderRun, Rollout.id == GraderRun.rollout_id)
                .join(SeedTask, Rollout.task_id == SeedTask.id)
                .order_by(Rollout.iteration, SeedTask.task_id)
                .all()
            )

            assert len(score_evolution) == 4
            # Verify we can see the improvement trajectory
            rest_api_scores = [row[1] for row in score_evolution if "rest_api" in row[2]]
            assert len(rest_api_scores) == 2
            assert rest_api_scores[1] > rest_api_scores[0]  # Iteration 1 > Iteration 0
