# Spawn Multi-Agent Team

Create a multi-agent team with isolated Git worktrees.

## Instructions

1. Generate team and create infrastructure:
   ```bash
   generate-team "Your task description"  # -> happy-ant-20240319-143052
   ```

   This creates:
   - Team directory at `~/.claude/teams/${TEAM_ID}`
   - Communication channel `channel.jsonl`
   - Team branch `team/${TEAM_ID}` from HEAD
   - Saves current dirty state
   - Worktree base directory

2. For each agent you need:
   - Create agent branch from team branch
   - Create worktree at `~/.claude/worktrees/${TEAM_ID}/${AGENT_NAME}`
   - Apply dirty state to worktree
   - Create scratch directory at `scratch/${TEAM_ID}/${AGENT_NAME}/`

4. Spawn monitor agent using Task tool:
   ```
   Task: {
     description: "${TEAM_ID}-monitor: Progress monitor",
     prompt: "Monitor ~/.claude/teams/${TEAM_ID}/channel.jsonl. Send desktop notifications every 5 minutes with team progress. Exit when all agents complete or after 10 min of inactivity."
   }
   ```

5. Spawn each worker agent using Task tool:
   ```
   Task: {
     description: "${TEAM_ID}-${AGENT_NAME}: ${ROLE}",
     prompt: "You are ${TEAM_ID}-${AGENT_NAME}. Work in ~/.claude/worktrees/${TEAM_ID}/${AGENT_NAME}. Your branch is agent/${TEAM_ID}/${AGENT_NAME}. Communicate via ~/.claude/teams/${TEAM_ID}/channel.jsonl. ${SPECIFIC_TASK}"
   }
   ```

6. Spawn final merger agent:
   ```
   Task: {
     description: "${TEAM_ID}-merger: Final integration",
     prompt: "Monitor channel until all complete. Then merge all agent branches into team branch, resolve conflicts, merge to main."
   }
   ```

7. Log team creation and exit:
   ```bash
   echo "Team ${TEAM_ID} spawned with ${AGENT_COUNT} agents"
   ```

## Example Decomposition

For "Build authentication system with tests":
- analyzer: Decompose requirements
- designer: Design API and architecture
- implementer: Build the system
- tester: Write comprehensive tests
- documenter: Create documentation
- critic: Review all outputs
