# Spawn Multi-Agent Team

Execute complex tasks using parallel agent teams with shared communication.

**⚠️ CRITICAL: The Task tool BLOCKS! You MUST spawn ALL agents in ONE Task call!**

**🚨 CRITICAL SPAWN PATTERN 🚨**

WRONG (orchestrator gets stuck forever):
```
Task: spawn monitor     # Blocks here
Task: spawn analyzer    # NEVER REACHED!
Task: spawn parser      # NEVER REACHED!
```

RIGHT (all agents spawn):
```
Task: [
  {description: "monitor...", prompt: "..."},
  {description: "analyzer...", prompt: "..."},
  {description: "parser...", prompt: "..."},
  {description: "ALL OTHER AGENTS", prompt: "..."}
]
```

## Instructions

1. **Analyze the task**
   - Understand the overall objective and constraints
   - Identify components that can be parallelized
   - Determine dependencies between subtasks
   - Create a mental model of the workflow DAG

2. **Create the team using ai-teams**
   ```bash
   # Create team with task description
   TEAM_ID=$(ai-teams create-team "$TASK_DESCRIPTION")
   echo "Created team: $TEAM_ID"
   ```

   This automatically:
   - Creates team branch from current HEAD
   - Saves any dirty state
   - Sets up team infrastructure in ~/.ai-teams/
   - Initializes communication channel

3. **Design workflow decomposition**
   - List all major work streams
   - For each stream, identify:
     - Required skills/role
     - Input dependencies
     - Expected outputs
     - Estimated complexity
   - Create execution graph with maximum parallelism

4. **CRITICAL: Task Tool Limitation**

   ⚠️ **The Task tool BLOCKS until all spawned agents complete!**

   This means you MUST spawn ALL agents in a SINGLE Task call. The workflow is:
   - Serial: Orchestrator analyzes and plans
   - **Parallel: ONE Task call spawning ALL agents** (orchestrator blocks here)
   - Serial: Orchestrator reviews results
   - Parallel: Another Task call if needed

   **DO NOT spawn agents one by one - you'll get stuck!**

5. **Spawn ALL agents in ONE call**
   ```
   Task: [
     {
       description: "$TEAM_ID-monitor: Team progress monitor",
       prompt: "/agent-boot-monitor $TEAM_ID"
     },
     {
       description: "$TEAM_ID-analyzer: Requirements analyzer",
       prompt: "/agent-boot $TEAM_ID analyzer\n\n[Role details...]"
     },
     {
       description: "$TEAM_ID-parser: Markdown parser developer",
       prompt: "/agent-boot $TEAM_ID parser\n\n[Role details...]"
     },
     {
       description: "$TEAM_ID-checker: Async link checker",
       prompt: "/agent-boot $TEAM_ID checker\n\n[Role details...]"
     },
     {
       description: "$TEAM_ID-cli-dev: CLI interface developer",
       prompt: "/agent-boot $TEAM_ID cli-dev\n\n[Role details...]"
     },
     {
       description: "$TEAM_ID-tester: Test suite developer",
       prompt: "/agent-boot $TEAM_ID tester\n\n[Role details...]"
     }
   ]
   ```

6. **After agents complete**
   Once the Task call returns (all agents done), you can:
   - Review the team branch for integrated work
   - Spawn additional agents if needed (e.g., integrator)
   - Handle any issues that arose

7. **Monitor team progress**
   - Use `ai-teams channel $TEAM_ID` to view real-time updates
   - Use `ai-teams list` to see all active teams
   - Watch for BLOCKER messages that need attention
   - Ensure all HANDOFF requests are accepted

8. **Team coordination**
   All agents communicate via the centralized channel:
   - STATUS updates every 5 minutes (required)
   - PROGRESS when completing subtasks
   - DISCOVERY for important findings
   - BLOCKER when stuck
   - HANDOFF to request help from specific agent
   - COMPLETE when finished with clean worktree

## Example Task Decomposition

For "Build authentication system with tests", spawn ALL in ONE Task call:
- **monitor**: Track progress (with early exit if no agents)
- **analyzer**: Analyze requirements and design API interface
- **implementer**: Build the authentication system
- **tester**: Write comprehensive unit and integration tests
- **documenter**: Create API documentation and usage examples
- **reviewer**: Review code quality and security

Remember: ALL these agents must be in a SINGLE Task array!

## Key Points

- Team infrastructure is managed by `ai-teams` package
- Each agent gets isolated Git worktree via `/agent-boot`
- Communication via append-only JSONL channel
- Monitor agent tracks progress and sends notifications
- Integration agent merges all work at the end

## Usage

```
/spawn "Build a REST API with authentication, tests, and documentation"
```

The orchestrator (you) will:
1. Create team with `ai-teams create-team`
2. Analyze task and identify ALL needed agent roles
3. **Spawn ALL agents in ONE Task call** (array of agent specs)
4. Wait for Task to complete (all agents finish)
5. Review results via `ai-teams channel`
6. Optionally spawn additional agents in a new Task call

**Critical Workflow Pattern:**
```
Serial (plan) → Parallel (spawn all) → Serial (review) → Parallel (if needed)
```

Never try to spawn agents one by one!
