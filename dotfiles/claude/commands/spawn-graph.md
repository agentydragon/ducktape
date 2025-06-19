# Spawn Graph - Parallel Execution of Dependency Graphs

Execute a complex task by breaking it into a dependency graph and spawning parallel agents to work through it at maximum throughput.

## Usage

```
/spawn-graph
```

Use this command when you have a complex task that can be broken into subtasks with dependencies between them.

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
