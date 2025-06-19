# Spawn Graph - Parallel Execution of Dependency Graphs

Execute a complex task by breaking it into a dependency graph and spawning parallel agents to work through it at maximum throughput.

## Usage

```
/spawn-graph [--workflow=naive|worktree]
```

Or conversationally anywhere in your prompt:
```
"analyze this monorepo, find if I can delete service X /spawn-graph oh and make it thorough"
"refactor the auth system /spawn-graph using the worktree workflow please"
```

Use this command when you have a complex task that can be broken into subtasks with dependencies between them. The command can appear at the start, middle, or end of your message.

## Workflow Modes

### 1. Naive Workflow (Default)
All tasks work in the same repository. Simple and direct, suitable for smaller dependency graphs or when git isolation isn't needed.

### 2. Worktree Workflow
Each task gets its own git worktree for complete isolation. Better for complex graphs, concurrent development, and when you need clean merging of parallel work.

Choose based on your needs:
- **Naive**: Quick, simple tasks without complex merge requirements
- **Worktree**: Complex tasks, need isolation, want clean git history per task

## What It Does

1. **Analyzes** the current task/request to identify all component pieces
2. **Creates** an explicit dependency graph showing what depends on what
3. **Executes** in waves:
   - Identifies all tasks with satisfied prerequisites
   - Spawns a batch of agents to work on them in parallel
   - Waits for completion
   - Repeats until all tasks are done
4. **Coordinates** results back into a coherent whole

## Process

### Phase 1: Graph Construction
1. Break down the task into atomic subtasks
2. Identify dependencies between subtasks
3. Create a DAG (Directed Acyclic Graph) representation
4. Validate no circular dependencies exist
5. Identify the critical path

### Phase 2: Execution Planning
1. Topologically sort the graph
2. Group tasks into execution waves
3. Estimate resource requirements
4. Plan agent allocation strategy

### Phase 3: Parallel Execution

Use the Task tool to spawn multiple agents in parallel:

```
while (incomplete tasks exist):
    ready_tasks = find_all_tasks_with_met_dependencies()

    # Spawn all ready tasks in ONE message with multiple Task tool calls
    results = parallel_task_execution([
        Task(description=f"Task {task.id}", prompt=task.prompt)
        for task in ready_tasks
    ])

    collect_results(results)
    update_completion_status()
```

**CRITICAL**: The key is to use multiple Task tool invocations in a SINGLE message. This spawns multiple agents that work in parallel.

### Phase 4: Integration
1. Collect all agent outputs
2. Resolve any conflicts
3. Integrate results into final deliverable
4. Generate summary report

## Worktree Workflow Details

When using `--workflow=worktree` (or requesting worktree workflow conversationally), the system creates an isolated git environment for maximum parallelism and clean merging.

### Key Definitions

- **Graph Instance Directory**: `./spawn-graph/{timestamp}-{description}/` - The root directory for a specific spawn-graph execution (e.g., `./spawn-graph/2025-01-02-1430-parallel-fizzbuzz/`)
- **Phase Directory**: `{graph-instance-dir}/phase{N}-{phasename}/` - Groups all tasks for a specific execution phase (e.g., `phase01-analysis/`, `phase02-compute/`)
- **Task Directory**: `{phase-dir}/task{M}-{taskname}/` - The git worktree where a task agent operates (e.g., `task01-fizzbuzz-1-33/`)
- **Task Output Dir**: `{task-dir}/spawn-graph/{timestamp}-{description}/phase{N}-{phasename}/task{M}-{taskname}/` - Subdirectory within the task directory for progress notes and final output. The path components after spawn-graph/ are duplicated to prevent merge conflicts.

### Scaffolding Structure

```
./spawn-graph/
├── README.md                                    # Explains this is for coordinating spawn-graph tasks
└── 2025-01-02-1234-improve-security-protocol/  # {graph-instance-dir}
    ├── TASK.md                                 # Full description of overall task
    ├── PLAN.md                                 # Dependency graph and execution phases
    ├── phase01-foundation/                     # {phase-dir} for Phase 1 (holds task worktrees)
    │   ├── task01-define-interfaces/           # {task-dir} for Phase 1 Task 1 (git worktree, checked out to branch spawn-graph/2025-01-02-1234-improve-security-protocol/phase01-foundation/task01-define-interfaces)
    │   │   ├── .git/                          # Git metadata (worktree link)
    │   │   ├── [project files]                # Actual code being worked on
    │   │   └── spawn-graph/                   # {task-output-dir} begins here
    │   │       └── 2025-01-02-1234-improve-security-protocol/
    │   │           └── phase01-foundation/
    │   │               └── task01-define-interfaces/
    │   │                   ├── PROGRESS.md    # Running notes
    │   │                   └── OUTPUT.md      # Final result
    │   ├── task02-security-audit/
    │   └── task03-threat-model/
    └── phase02-implementation/                 # {phase-dir} for Phase 2 (holds task worktrees)
        ├── task01-auth-module/
        └── task02-encryption-layer/
```

**KEY INSIGHT**: The task output dir is intentionally duplicated! When branches merge, each task's output lands in a unique location, preventing conflicts.

### Workflow Process

1. **Initialization**
   - Create `./spawn-graph/` directory with README explaining its purpose
   - Create graph instance directory: `./spawn-graph/{timestamp}-{task-description}/`
   - Write `TASK.md` with full task description
   - Analyze dependencies and create `PLAN.md` with:
     - List of all subtasks
     - Dependency edges (DAG)
     - Optimized phases for minimal serial execution
     - Each task named like `phase01-foundation/task03-check-dependencies` (format: phase{N}-{phasename}/task{M}-{taskname})

2. **Worktree Setup per Task**
   - Create worktree at `./spawn-graph/{instance}/phase{N}-{phasename}/task{M}-{taskname}/`
   - Create branch: `spawn-graph/{instance}/phase{N}-{phasename}/task{M}-{taskname}`
   - If current directory is dirty, apply same uncommitted changes to each worktree
   - Task output dir will be created by agent at: `./spawn-graph/{instance}/phase{N}-{phasename}/task{M}-{taskname}/spawn-graph/{instance}/phase{N}-{phasename}/task{M}-{taskname}/`

3. **Phase Execution**
   For each phase:
   - Launch N parallel agents (one per task in phase)
   - Each agent receives:
     ```
     Read TASK.md and PLAN.md from ./spawn-graph/{instance}/
     Execute task phase{X}-{phasename}/task{Y}-{taskname}
     Work exclusively in worktree ./spawn-graph/{instance}/phase{X}-{phasename}/task{Y}-{taskname}/
     Create task output dir at: ./spawn-graph/{instance}/phase{X}-{phasename}/task{Y}-{taskname}/spawn-graph/{instance}/phase{X}-{phasename}/task{Y}-{taskname}/
     Make logical commits on branch spawn-graph/{instance}/phase{X}-{phasename}/task{Y}-{taskname}
     Write final OUTPUT.md in task output dir when done/blocked
     ```

4. **Agent Work Pattern**
   - Read overall task and plan
   - Work only in assigned worktree
   - Create task output directory (with full path duplication)
   - Keep current state in task output directory
   - Make incremental commits including task output
   - Document observations, side outputs, blockers
   - Final output goes to task output directory's `OUTPUT.md` with status:
     - SUCCESS: Task completed
     - FAILED: Task cannot be completed
     - BLOCKED: Waiting on dependency or external factor
     - PARTIAL: Some progress made but incomplete

5. **Phase Completion**
   - Read each task's `OUTPUT.md` from branch tip
   - Merge successful task branches into main branch
   - Handle conflicts intelligently (worst case: drop unmergeable work)
   - Update `PLAN.md` if needed (add retries, conflict resolution tasks)
   - Clean up worktrees with `git worktree remove`
   - Proceed to next phase

### Example: Parallel FizzBuzz Computation

**Graph Instance**: `./spawn-graph/2025-01-02-1430-parallel-fizzbuzz/`

**TASK.md**:
```markdown
# Parallel FizzBuzz Analysis
Generate FizzBuzz for numbers 1-100 with parallel computation and analysis
```

**PLAN.md**:
```markdown
# Execution Plan

## Dependency Graph
```
phase01-analysis
├── task01-range-partition    → phase02-compute/task01, task02, task03
├── task02-pattern-study       → phase02-compute/task04
└── task03-optimization-plan   → phase02-compute/task04

phase02-compute  
├── task01-fizzbuzz-1-33      → phase03-merge/task01
├── task02-fizzbuzz-34-66     → phase03-merge/task01  
├── task03-fizzbuzz-67-100    → phase03-merge/task01
└── task04-optimized-algo      → phase03-merge/task02

phase03-merge
├── task01-combine-results     → phase04-analysis/task01
└── task02-benchmark           → phase04-analysis/task01

phase04-analysis
└── task01-final-report
```

## Phases
- Phase 1: Analysis and planning (3 parallel tasks)
- Phase 2: Computation (4 parallel tasks)
- Phase 3: Merging and benchmarking (2 parallel tasks)
- Phase 4: Final analysis (1 task)
```

**Parallel Execution Visualization**:
```
Time →
T0: [Analysis Task 1] [Analysis Task 2] [Analysis Task 3]
T1: [Compute 1-33] [Compute 34-66] [Compute 67-100] [Optimized Algo]
T2: [Merge Results] [Benchmark]
T3: [Final Report]
```

**Agent Work Example (Phase 2, Task 1)**:
```bash
# Assume we start in /home/user/myproject
cd ./spawn-graph/2025-01-02-1430-parallel-fizzbuzz/phase02-compute/task01-fizzbuzz-1-33/

# We are now in {task-dir} for Phase 2, Task 1
# pwd is /home/user/myproject/spawn-graph/2025-01-02-1430-parallel-fizzbuzz/phase02-compute/task01-fizzbuzz-1-33/

# Create {task-output-dir} for this task (note the path duplication!)
mkdir -p spawn-graph/2025-01-02-1430-parallel-fizzbuzz/phase02-compute/task01-fizzbuzz-1-33/

# The full path of {task-output-dir} is:
# /home/user/myproject/spawn-graph/2025-01-02-1430-parallel-fizzbuzz/phase02-compute/task01-fizzbuzz-1-33/spawn-graph/2025-01-02-1430-parallel-fizzbuzz/phase02-compute/task01-fizzbuzz-1-33/

# Write progress (append to track history)
cat >> spawn-graph/2025-01-02-1430-parallel-fizzbuzz/phase02-compute/task01-fizzbuzz-1-33/PROGRESS.md << EOF
## Progress update - commit $(git rev-parse --short HEAD) @ $(date -u +"%Y-%m-%d %H:%M:%S UTC")

- Starting FizzBuzz computation for range 1-33
- Implementing standard algorithm
- Added unit tests
- Performance optimizations applied

EOF

# Do the actual work
cat > src/fizzbuzz_1_33.py << 'EOF'
def fizzbuzz_range_1_33():
    results = []
    for i in range(1, 34):
        if i % 15 == 0:
            results.append("FizzBuzz")
        elif i % 3 == 0:
            results.append("Fizz")
        elif i % 5 == 0:
            results.append("Buzz")
        else:
            results.append(str(i))
    return results
EOF

# Commit work
git add -A
git commit -m "feat: implement fizzbuzz for range 1-33"

# Write tests
cat > tests/test_fizzbuzz_1_33.py << 'EOF'
# ... test implementation ...
EOF

git add -A
git commit -m "test: add comprehensive test coverage"

# Final output goes in task output directory
# We're still in {task-dir}, so we write to the relative path:
cat > spawn-graph/2025-01-02-1430-parallel-fizzbuzz/phase02-compute/task01-fizzbuzz-1-33/OUTPUT.md << 'EOF'
STATUS: SUCCESS

Generated FizzBuzz for numbers 1-33
- Implementation: src/fizzbuzz_1_33.py
- Tests: tests/test_fizzbuzz_1_33.py
- Test coverage: 100%
- Performance: 0.002s for range

Results preview:
1, 2, Fizz, 4, Buzz, Fizz, 7, 8, Fizz, Buzz, 11, Fizz, 13, 14, FizzBuzz...
EOF

# The full path of OUTPUT.md is:
# /home/user/myproject/spawn-graph/2025-01-02-1430-parallel-fizzbuzz/phase02-compute/task01-fizzbuzz-1-33/spawn-graph/2025-01-02-1430-parallel-fizzbuzz/phase02-compute/task01-fizzbuzz-1-33/OUTPUT.md

git add -A
git commit -m "docs: add final output and results"
```

### Phase Completion: Reviewing and Merging Results

**Example: After Phase 2 completes, before starting Phase 3**:
```bash
# Starting from project root (/home/user/myproject)
cd spawn-graph/2025-01-02-1430-parallel-fizzbuzz/phase02-compute/

# Review all task outputs
for task in task*/; do
    echo "=== Output from $task ==="
    cat "$task/spawn-graph/2025-01-02-1430-parallel-fizzbuzz/phase02-compute/$task/OUTPUT.md"
    echo
done

# Example output:
# === Output from task01-fizzbuzz-1-33/ ===
# STATUS: SUCCESS
# Generated FizzBuzz for numbers 1-33
# ...
# === Output from task02-fizzbuzz-34-66/ ===
# STATUS: SUCCESS
# Generated FizzBuzz for numbers 34-66
# ...
# === Output from task03-fizzbuzz-67-100/ ===
# STATUS: BLOCKED
# Could not complete due to missing dependency X
# ...
# === Output from task04-optimized-algo/ ===
# STATUS: SUCCESS
# Implemented optimized algorithm with 3x speedup
# ...

# Make planning decisions based on outputs
# - task01, task02, task04: SUCCESS → merge their branches
# - task03: BLOCKED → add retry task to Phase 3

# Merge successful task branches
cd ../../.. # Back to project root
git checkout main

# Merge each successful task
for task in phase02-compute/task01-fizzbuzz-1-33 \
           phase02-compute/task02-fizzbuzz-34-66 \
           phase02-compute/task04-optimized-algo; do
    branch="spawn-graph/2025-01-02-1430-parallel-fizzbuzz/$task"
    echo "Merging $branch..."
    git merge --no-ff "$branch" -m "Merge $task from spawn-graph"
done

# Update PLAN.md for Phase 3 to include retry of blocked task
cat >> spawn-graph/2025-01-02-1430-parallel-fizzbuzz/PLAN.md << 'EOF'

## Phase 3 Adjustments
- Added task03-retry-fizzbuzz-67-100 to handle blocked task from Phase 2
EOF

# Clean up completed worktrees
for task in phase02-compute/task*/; do
    git worktree remove "spawn-graph/2025-01-02-1430-parallel-fizzbuzz/$task"
done
```

**After Merging All Successful Tasks**:
```
[main branch after merge]
├── src/
│   ├── fizzbuzz_1_33.py
│   ├── fizzbuzz_34_66.py
│   ├── fizzbuzz_67_100.py         # Missing due to blocked task
│   └── fizzbuzz_optimized.py
├── tests/
│   └── [test files]
└── spawn-graph/2025-01-02-1430-parallel-fizzbuzz/
    └── phase02-compute/
        ├── task01-fizzbuzz-1-33/
        │   ├── PROGRESS.md
        │   └── OUTPUT.md
        ├── task02-fizzbuzz-34-66/
        │   ├── PROGRESS.md
        │   └── OUTPUT.md
        ├── task03-fizzbuzz-67-100/
        │   ├── PROGRESS.md
        │   └── OUTPUT.md          # Shows BLOCKED status
        └── task04-optimized-algo/
            ├── PROGRESS.md
            └── OUTPUT.md
```

### Benefits of Path Duplication

1. **Conflict-Free Merges**: Each task's output has a unique path
2. **Complete History**: All task outputs preserved in final merge
3. **Easy Navigation**: Can review any task's work in isolation
4. **Debugging**: Full paper trail of what each agent did
5. **Reusability**: Can cherry-pick specific task implementations

### When to Use Worktree vs Naive

**Use Worktree Workflow when**:
- Complex refactoring across many files
- High risk of merge conflicts
- Need clean git history per subtask
- Want ability to cherry-pick specific task results
- Running many tasks in parallel (>5)
- Need full audit trail of parallel work

**Use Naive Workflow when**:
- Simple task decomposition
- Low conflict risk
- Quick experiments
- Tasks mostly read-only or in different areas
- Don't need isolated git history

## Example Execution

Given a task like "Refactor the client library to use modern patterns":

### Generated Graph:
```
A1: Define new interfaces (2d)
├─→ A2: Create type system (3d)
│   ├─→ A3: Implement core classes (4d)
│   └─→ A4: Build validators (2d)
├─→ B1: Design API surface (2d)
│   └─→ B2: Implement API (5d)
└─→ C1: Plan migration (1d)
    └─→ C2: Write migration tools (3d)

Critical Path: A1 → A2 → A3 = 9 days
Parallel Path: 4-5 days with 3 agents
```

### Execution Waves:
- **Wave 1**: Spawn 1 agent for A1
- **Wave 2**: Spawn 3 agents for A2, B1, C1
- **Wave 3**: Spawn 3 agents for A3, A4, B2
- **Wave 4**: Spawn 1 agent for C2

## Implementation

The actual implementation uses Claude's Task tool to spawn parallel agents:

### Step 1: Analyze and Create Graph
First, analyze the task and create the dependency graph with all subtasks clearly defined.

### Step 2: Execute in Waves
For each wave, use multiple Task tool invocations in a SINGLE message:

```
# Example of spawning Wave 1 with 15 parallel tasks:
<multiple_tool_use>
  <invoke name="Task">
    <parameter name="description">A1.1 Basic Types</parameter>
    <parameter name="prompt">Create NodeId, EdgeId, WorkspaceId type definitions...</parameter>
  </invoke>
  <invoke name="Task">
    <parameter name="description">A1.2 Core Interfaces</parameter>
    <parameter name="prompt">Define INode, IEdge, IStore interfaces...</parameter>
  </invoke>
  <invoke name="Task">
    <parameter name="description">B1.1 WebSocket Wrapper</parameter>
    <parameter name="prompt">Implement WebSocket connection wrapper...</parameter>
  </invoke>
  ... (12 more Task invocations)
</multiple_tool_use>
```

### Step 3: Collect Results
Each Task tool returns a result. Process these results and determine the next wave of ready tasks.

### Step 4: Repeat Until Complete
Continue spawning waves of parallel tasks until the entire graph is processed.

## Agent Instructions Template

Each spawned agent receives:

```markdown
You are agent {agent_id} working on task {task_id}.

## Your Task
{task_description}

## Dependencies Completed
{completed_dependencies_and_outputs}

## Your Deliverables
{expected_outputs}

## Integration Points
{how_your_output_connects_to_other_tasks}

## Constraints
- Time limit: {estimated_duration}
- Must produce: {output_format}
- Must coordinate with: {related_agents}
```

## Best Practices

1. **Granularity**: Break tasks down to 1-4 hour chunks for optimal parallelism
2. **Dependencies**: Make dependencies explicit, not implicit
3. **Interfaces**: Define clear interfaces between tasks
4. **Checkpoints**: Built-in validation at wave boundaries
5. **Fallbacks**: Have strategies for agent failures

## When to Use

Perfect for:
- Large refactoring projects
- Multi-component system design
- Complex documentation tasks
- Research projects with multiple threads
- Any task with natural parallelism

Not suitable for:
- Strictly sequential tasks
- Tasks requiring continuous context
- Small tasks (< 2 hours)
- Tasks with unclear requirements

## Output Format

The command produces:
1. Dependency graph visualization
2. Execution plan with timeline
3. Wave-by-wave progress updates
4. Final integrated result
5. Performance metrics (speedup achieved)

## Advanced Features

### Resource Constraints
```
/spawn-graph --max-agents=5 --memory-limit=8GB
```

### Priority Scheduling
```
/spawn-graph --optimize=critical-path
/spawn-graph --optimize=resource-usage
```

### Checkpoint Recovery
```
/spawn-graph --checkpoint=every-wave
/spawn-graph --resume-from=wave-3
```

## How It Actually Works

The magic happens through Claude's ability to invoke multiple Task tools in parallel:

1. **Single Message, Multiple Tasks**: When you invoke the Task tool multiple times in one message, Claude spawns that many agents to work in parallel.

2. **True Parallelism**: Each Task tool invocation creates an independent agent that works on its assigned task without blocking others.

3. **Result Collection**: All agents return their results, which can then be processed to determine the next wave.

### Example Execution Pattern

```
User: /spawn-graph

Claude: I'll execute the first wave of 15 independent tasks:
[Invokes 15 Task tools in one message]

[15 agents work in parallel]

[Results returned]

Claude: Wave 1 complete. Based on results, Wave 2 has 20 ready tasks:
[Invokes 20 Task tools in one message]

[Process continues until graph is complete]
```

## Related Commands

- `/spawn` - Simple multi-agent parallelism without dependency management
- `/plan` - Create execution plan without spawning agents
- `/coordinate` - Manage already-running parallel agents

## Implementation Note

This command leverages Claude's Task tool capability:
1. **Dependency graph analysis** - Break down complex tasks into DAG
2. **Topological sorting** - Determine execution order
3. **Wave-based execution** - Group tasks by dependency level
4. **Parallel Task invocation** - Use multiple Task tools in one message
5. **Result integration** - Combine outputs from all agents

The key innovation is using multiple Task tool invocations in a single message to achieve true parallel execution while respecting dependencies. Each wave spawns N agents where N is the number of ready tasks.
