# Claude Instruction Optimizer - System Overview

## What It Does

Iteratively improves CLAUDE.md system prompts for coding agents through containerized execution, automated grading, and AI-powered prompt engineering.

## Architecture

### Data Flow

```
YAML Config → Database → Docker Containers → Claude Execution → OpenAI Grading → Pattern Analysis → Improved Prompts
```

### Key Components

- **Containerized Execution**: Isolated Docker environments per programming language
- **Database Integration**: SQLite storage for complete audit trails
- **OpenAI o3 Grading**: Structured evaluation with reasoning capture
- **Pattern Analysis**: AI-powered failure analysis and prompt improvement
- **Statistical Tracking**: Score evolution with confidence intervals

---

## Package Structure

```
src/adgn_llm/instruction_optimizer/
├── config/settings.py          # Configuration management
├── core/
│   ├── containerized_claude.py # Container execution wrapper
│   ├── optimizer.py            # Main optimization algorithm
│   ├── yaml_loader.py          # YAML → Database sync
│   ├── logging_utils.py        # Structured logging
│   ├── truncation_utils.py     # Token limit management
│   └── plots.py               # Score visualization
├── database/
│   ├── models.py              # SQLAlchemy schema
│   └── database_service.py    # Database operations
└── docker/
    └── build.py               # Image build automation
```

---

## Database Schema

### Core Tables

- **OptimizationRun**: Top-level experiment tracking
- **SystemPrompt**: CLAUDE.md content versioning with reasoning
- **SeedTask**: Programming tasks with docker_image specification
- **Rollout**: Individual agent executions with metadata
- **RolloutMessage**: Complete Claude conversation sequences
- **RolloutFile**: Agent-created file metadata
- **GraderRun**: OpenAI o3 evaluation results
- **PatternAnalysis**: AI-generated improvement insights

### Key Relationships

```
OptimizationRun → SystemPrompt → Rollout → GraderRun
SeedTask → Rollout → RolloutMessage + RolloutFile
```

---

## Docker Architecture

### Image Hierarchy

```
ubuntu:22.04
└── claude-base:latest
    ├── claude-dev:python  # Python + common packages
    ├── claude-dev:rust    # Rust toolchain
    ├── claude-dev:go      # Go compiler
    ├── claude-dev:ruby    # Ruby + gems
    └── claude-dev:node    # Node.js + npm
```

### Container Execution

1. **Image Selection**: Task specifies `docker_image: "claude-dev:python"`
2. **Volume Mounts**: Workspace (rw), logs (rw), git repos (ro)
3. **Pre-Task Commands**: Shell scripts for git cloning, setup
4. **Claude Execution**: Containerized Claude Code CLI
5. **File Collection**: Bind mount scanning with exclusion patterns

---

## Configuration

### Core Settings (`config.yaml`)

```yaml
rollouts:
  max_parallel: 16 # Concurrent executions
  max_turns: 100 # Claude conversation limit

grader:
  model: "o3" # OpenAI model
  reasoning_effort: "medium" # o3 reasoning level

tokens:
  max_context_tokens: 150000 # Input limit
  max_files_tokens: 100000 # File content limit

exclude_patterns: # Gitignore-style patterns
  - "*.log"
  - "**/__pycache__/**"
  - "**/.git/**"
```

### Task Definition (`seeds.yaml`)

```yaml
- id: my_task
  prompt: "Create a web scraper..."
  docker_image: "claude-dev:python"
  pre_task_commands: |
    git clone /git/repo1 ./project
    cd project && npm install
```

---

## Optimization Algorithm

### Main Loop

```python
for iteration in range(max_iterations):
    # 1. Get/generate system prompt
    prompt = get_system_prompt(iteration)

    # 2. Run parallel rollouts
    rollouts = await execute_parallel_rollouts(tasks, prompt)

    # 3. Grade with OpenAI o3
    grades = await grade_rollouts(rollouts)

    # 4. Analyze patterns
    analysis = await analyze_patterns(grades)

    # 5. Generate improved prompt
    next_prompt = await improve_prompt(analysis)
```

### Grading System

- **Structured Output**: OpenAI o3 function calling with dynamic schemas
- **Multi-Facet Scoring**: Per-criterion evaluation (0-10 scale)
- **Reasoning Capture**: Full o3 reasoning stored for analysis
- **Statistical Aggregation**: Mean, std dev, confidence intervals

---

## File Locations

### Configuration Files

```
config.yaml                 # Main configuration
data/seeds.yaml            # Programming tasks
data/graders_consolidated.yaml  # Grading criteria
```

### Docker Files

```
docker/claude-base/Dockerfile   # Base image
docker/python/Dockerfile       # Python environment
docker/rust/Dockerfile         # Rust environment
# ... other language environments
```

### Output Structure

```
agent_output/{timestamp}/
├── iter_1/CLAUDE.md           # System prompt used
├── iter_1/task_{id}/agent_{n}/ # Rollout outputs
├── score_evolution.png        # Performance plots
└── final_score_evolution_report.txt
```

---

## Key Technical Details

### Security Model

- Containers run as non-root user (UID 1000)
- Git repositories mounted read-only
- PATH isolation prevents host tool access
- Tini PID 1 for proper process management

### Performance Optimizations

- Parallel rollout execution with semaphore control
- Bind mounts instead of docker cp for file access
- Token-aware content truncation for API efficiency
- Docker buildx caching for fast image builds

### Error Handling

- Fail-fast rollout execution with proper cleanup
- Real-time cost tracking with interrupt handling
- Container death detection and remediation
- Comprehensive logging with structured output

---

## Usage

### Build Docker Images

```bash
python -m adgn_llm.instruction_optimizer.docker.build
```

### Run Optimization

```bash
python -m adgn_llm.instruction_optimizer.core.optimizer \
  --iterations 10 \
  --rollouts-per-task 3 \
  --max-parallel 16
```

---

## Critical Files to Preserve

- **DEBUGGING.md**: Container troubleshooting knowledge
- **config.yaml**: System configuration
- **data/seeds.yaml**: Task definitions
- **data/graders_consolidated.yaml**: Evaluation criteria

These files contain hard-won knowledge and should be preserved across system changes.
