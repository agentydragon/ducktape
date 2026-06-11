# Claude Instruction Optimizer

A parallel prompt optimization system for coding agents that iteratively improves system prompts by running multiple agent rollouts, grading solutions with OpenAI's o3 model, and using AI-powered prompt engineering.

## Overview

Runs coding agents on programming tasks, grades their solutions, and uses those results to automatically improve the system prompt.
Uses Docker for isolated execution and stores results in a database for analysis.

**Key Features:**

- Parallel rollouts
- Docker containerization for isolated agent execution
- OpenAI o3 model for grading and prompt engineering
- Comprehensive logging and cost tracking
- Real-time score evolution plots

## Quick Start

### Run Optimization

**Production Run (Recommended):**

```bash
python3 -m adgn_llm.instruction_optimizer.core.optimizer \
  --iterations 10 \
  --rollouts-per-task 3 \
  --tasks-per-iteration 10 \
  --max-parallel 8 \
  --mode summary
```

- 10 iterations of prompt improvement
- 3 rollouts per task for statistical reliability
- 10 random tasks per iteration (faster than all 25)
- 8 concurrent rollouts (30 total per iteration)

## Usage

### Command Line Options

| Flag                    | Description                                   | Default         | Example                    |
| ----------------------- | --------------------------------------------- | --------------- | -------------------------- |
| `--iterations`          | Number of optimization iterations             | 10              | `--iterations 15`          |
| `--rollouts-per-task`   | Agent rollouts per task                       | 1               | `--rollouts-per-task 4`    |
| `--tasks-per-iteration` | Random tasks per iteration (with replacement) | All tasks       | `--tasks-per-iteration 10` |
| `--max-parallel`        | Maximum concurrent rollouts                   | 8               | `--max-parallel 16`        |
| `--mode`                | Processing mode: `full_rollouts` or `summary` | `full_rollouts` | `--mode summary`           |

## Architecture

Tasks can:

- Specify their required Docker image.
- Execute pre-task scripts to set up their environment, including:
  - Clone Git repos the task depends on from shared RW/RO volume
  - And/or GitHub if shared volume does not yet have a clone

## Output

### Run Directory Structure

```
agent_output/{timestamp}/
├── iter_1/CLAUDE.md              # System prompt for iteration 1
├── iter_1/task_X/agent_Y/        # Agent working directories
├── score_evolution.png           # Score trends over time
├── score_evolution_faceted.png   # Per-facet score evolution
├── openai_api_log.jsonl          # OpenAI API calls
└── anthropic_api_log.jsonl       # Anthropic API calls
```

### Database Storage

All results stored in `optimizer.db`:

- **optimization_runs**: Run metadata and configuration
- **system_prompts**: Generated prompts with reasoning
- **rollouts**: Individual agent executions with costs
- **rollout_messages**: Complete conversation logs
- **rollout_files**: Generated code files
- **grading_results**: Scores and rationales per facet

## Configuration Files

- **`seeds.yaml`**: Programming tasks with dependencies and git repos
- **`graders.yaml`**: Detailed grading criteria with 23 specific evaluators
- **`graders_consolidated.yaml`**: Condensed grading rubrics (6 broad categories)
- **`dependency_config.yaml`**: Docker layer definitions and capabilities
- **`optimizer_config.py`**: System configuration (timeouts, limits, etc.)

### Grading System

The optimizer uses two complementary grading configurations:

**`graders.yaml`** - High-resolution evaluation with 24 specific graders:

- Individual concerns like `exception_handling`, `nullable_types`, `enum_types`, `organization`
- Detailed evaluation criteria and examples for each grader
- Use when you need granular feedback on specific coding practices
- Err on the side of adding new graders here for specific issues

**`graders_consolidated.yaml`** - Broad evaluation with 7 consolidated categories:

- `type_safety_data_design`: Strong typing and data structures
- `code_quality_clarity`: Readability and modern language features
- `robustness_error_handling`: Defensive programming and error boundaries
- `architecture_design`: Separation of concerns and proper tools
- `implementation_completeness`: Full implementation without cruft
- `test_quality`: Meaningful tests that verify business logic
- `organization_hygiene`: Clean repository structure with concise documentation
- Err on the side of consolidating related concerns here

Choose based on your evaluation needs:

- **Development/debugging**: Use `graders.yaml` for specific feedback
- **Production evaluation**: Use `graders_consolidated.yaml` for broader assessment

## Development Setup

### Installation

```bash
# Install package with development dependencies
pip install -e ".[dev]"
```

### Code Quality

**Type Checking with MyPy:**

```bash
mypy src/adgn_llm/instruction_optimizer/core/optimizer.py
```

**Testing with Pytest:**

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src/adgn_llm

# Run specific test files
pytest tests/test_yaml_loader.py
```

**Code Formatting:**

```bash
# Format code
black src/ tests/

# Lint code
ruff check src/ tests/
ruff check --fix src/ tests/
```

## Requirements

- Docker with buildx support
- Python 3.11+ with required packages
- OpenAI API key (for o3 grading and prompt engineering)
- Anthropic API key (for Claude coding agents)

### Docker Buildx Setup (One-time)

**Install buildx** to avoid "legacy builder" warnings:

```bash
docker buildx install
```

**For macOS with Colima:**

```bash
# Create Docker CLI plugins directory
mkdir -p ~/.docker/cli-plugins

# Download and install buildx for macOS ARM64
curl -Lo ~/.docker/cli-plugins/docker-buildx https://github.com/docker/buildx/releases/download/v0.17.1/buildx-v0.17.1.darwin-arm64
chmod +x ~/.docker/cli-plugins/docker-buildx

# Install as default builder
docker buildx install

# Verify installation
docker buildx version
```

## Advanced Usage

### Pattern Analysis Mode

```bash
python3 -m adgn_llm.instruction_optimizer.core.optimizer --mode summary --iterations 5
```

Uses condensed pattern analysis instead of full rollout data for prompt engineering.

### Custom Parallelism

```bash
python3 -m adgn_llm.instruction_optimizer.core.optimizer --max-parallel 4
```

### Monitoring Costs

The system tracks costs in real-time. Use Ctrl+C for graceful interruption:

```
Interrupt received, reporting costs before exit
FINAL COST SUMMARY: total_cost_usd=45.67, rollout_count=120, avg_cost_per_rollout_usd=0.38
```

## Troubleshooting

### Docker Build Issues

```bash
# Check Docker BuildKit is enabled
export DOCKER_BUILDKIT=1

# Try building individual layers if main build fails
docker buildx build --target system-base -t claude-dev:system-base --load .
```
