# Spawn Multi-Agent Team

Execute complex tasks using parallel agent teams with shared communication.

## Instructions

1. **Analyze the task**
   - Understand the overall objective and constraints
   - Identify components that can be parallelized
   - Determine dependencies between subtasks
   - Create a mental model of the workflow DAG

2. **Generate your agent name**
   - Run `generate-agent-name` to get your orchestrator name
   - Example: If you get "clever-fox", all spawned agents will be "clever-fox-{role}"

3. **Create team directory structure**
   ```bash
   AGENT_NAME=$(generate-agent-name)
   TEAM_ID="${AGENT_NAME}-$(date +%Y%m%d-%H%M%S)"
   mkdir -p ~/.claude/teams/${TEAM_ID}
   touch ~/.claude/teams/${TEAM_ID}/channel.jsonl
   echo '{}' > ~/.claude/teams/${TEAM_ID}/dashboard.json

   # Create team branch from current HEAD (preserving dirty state)
   TEAM_BRANCH="team/${TEAM_ID}"
   git branch $TEAM_BRANCH HEAD
   echo "$TEAM_BRANCH" > ~/.claude/teams/${TEAM_ID}/team-branch.txt
   ```

4. **Initialize communication channel**
   - Create append-only log at `~/.claude/teams/${TEAM_ID}/channel.jsonl`
   - Each line is a valid JSON object
   - Example entry:
   ```json
   {"timestamp":"2024-03-19T10:30:45.123Z","agent":"clever-fox-analyzer","type":"STATUS","message":"Starting API analysis","data":{}}
   ```

5. **Design workflow decomposition**
   - List all major work streams
   - For each stream, identify:
     - Required skills/role
     - Input dependencies
     - Expected outputs
     - Estimated complexity
   - Create execution graph with maximum parallelism

6. **Create Git worktrees for each agent**
   ```bash
   # For each agent, create isolated worktree
   WORKTREE_BASE="$HOME/.claude/worktrees/${TEAM_ID}"
   mkdir -p "$WORKTREE_BASE"

   for AGENT in "${AGENTS[@]}"; do
     AGENT_BRANCH="agent/${TEAM_ID}/${AGENT}"
     AGENT_WORKTREE="$WORKTREE_BASE/${AGENT}"

     # Create agent branch from team branch
     git branch $AGENT_BRANCH $TEAM_BRANCH

     # Create worktree
     git worktree add "$AGENT_WORKTREE" "$AGENT_BRANCH"

     # Restore dirty state from main worktree
     # Save current dirty state
     git stash create > ~/.claude/teams/${TEAM_ID}/dirty-state.sha

     # Apply to agent worktree
     cd "$AGENT_WORKTREE"
     if [ -s ~/.claude/teams/${TEAM_ID}/dirty-state.sha ]; then
       git stash apply $(cat ~/.claude/teams/${TEAM_ID}/dirty-state.sha)
     fi
     cd -
   done
   ```

7. **Write task specifications**
   For each agent, create `~/.claude/teams/${TEAM_ID}/task-{agent-name}.md`:
   ```markdown
   # Task: [Specific Role]

   ## Context
   - Team ID: [team-id]
   - Your role: [specific responsibility]
   - Dependencies: [what you need from others]

   ## Git Workflow
   - Your worktree: ~/.claude/worktrees/[team-id]/[agent-name]
   - Your branch: agent/[team-id]/[agent-name]
   - Team branch: team/[team-id]
   - Your scratch directory: scratch/[team-id]/[agent-name]/
   - ALWAYS work in your worktree
   - Commit frequently with descriptive messages
   - Push to your branch when you have working changes
   - Pull and merge team branch periodically for coordination

   ## Scratch Directory Usage
   - Location: scratch/[team-id]/[agent-name]/
   - Purpose: Version-controlled workspace for your tools, scripts, experiments
   - Create it: mkdir -p scratch/[team-id]/[agent-name]
   - Use for: Test scripts, temporary tools, experiments, notes
   - DO NOT use for: Final deliverables (put those in proper locations)
   - Commit your scratch work to preserve it
   - Will be merged with your other changes

   ## Communication Protocol
   - Log: ~/.claude/teams/[team-id]/channel.jsonl
   - Report STATUS every 5 minutes (required)
   - Use proper message types (see below)

   ## Message Protocol

   ### Required Messages
   - STATUS: "Starting work" / "Working on X" / "Idle, waiting for Y"
   - PROGRESS: One-line summary of what you just accomplished
   - COMPLETE: Final summary when your task is done

   ### Coordination Messages
   - HANDOFF: "I need agent X to do Y" (include target_agent in data)
   - HANDOFF_ACCEPTED: "I accept handoff from X" (include source_agent)
   - BLOCKER: "Cannot proceed because X" (you must resolve this later)
   - BLOCKER_RESOLVED: "Blocker X resolved by doing Y"

   ### Information Sharing
   - DISCOVERY: Critical finding others must know
   - FYI: General information that might be useful
   - CRITIQUE: Feedback on another agent's work

   ## Deliverables
   - [Specific outputs expected]
   - Final work committed to your branch

   ## Instructions
   1. Navigate to your worktree: cd ~/.claude/worktrees/[team-id]/[agent-name]
   2. Read channel.jsonl to understand current state
   3. Post STATUS "Starting work on [task]"
   4. Execute your tasks, posting PROGRESS after each subtask
   5. Commit your work frequently to your branch
   6. Every 15-30 minutes:
      - Push your branch: git push origin [your-branch]
      - Pull team branch: git pull origin team/[team-id]
      - Merge if no conflicts: git merge team/[team-id]
      - Share discoveries by pushing to team branch (if stable)
   7. Post STATUS every 5 minutes even if idle
   8. Use HANDOFF when you need another agent
   9. When done: push final changes and post COMPLETE
   ```

7. **Spawn monitoring agent first**
   ```
   Task: {
     description: "monitor-progress: Team progress monitor",
     prompt: "Monitor ~/.claude/teams/${TEAM_ID}/channel.jsonl. Every 5 minutes: 1) Check all agents reported within 5 min, 2) Summarize progress since last notification, 3) Send desktop notification with: 'Team ${TEAM_ID}: X/Y agents active, Z complete. Current: [one-line summary]'. Exit conditions: a) No new entries for 10 min - send ABORT notification and exit, b) All agents COMPLETE or failed - send final summary and exit, c) All agents have active BLOCKER and no progress for 10 min - send 'All agents blocked' notification and exit."
   }
   ```

8. **Spawn worker agents**
   For each agent, use Task tool:
   ```
   Task: {
     description: "[agent-name]: [role description]",
     prompt: "Your name is [agent-name]. CRITICAL: cd to your worktree at ~/.claude/worktrees/[team-id]/[agent-name] FIRST. Read ~/.claude/teams/[team-id]/task-[agent-name].md. Follow all communication protocols. Post to channel.jsonl using echo '{}' >> channel.jsonl format. Work ONLY in your worktree, commit to your branch agent/[team-id]/[agent-name]."
   }
   ```

9. **Monitor team progress**
   - Watch channel.jsonl for important messages
   - Track BLOCKER messages
   - Verify HANDOFF/HANDOFF_ACCEPTED pairs
   - Ensure critic feedback is addressed

10. **Synthesize results**
    - Once all agents post COMPLETE
    - Spawn final merge agent:
    ```
    Task: {
      description: "[agent-name]-merger: Final integration",
      prompt: "cd ~/.claude/worktrees/[team-id]. Create merger worktree. For each agent branch: 1) git merge agent/[team-id]/[agent] into team branch, 2) Resolve any conflicts, 3) Test integrated changes. Finally: merge team branch back to original branch. Post COMPLETE with summary of integrated changes."
    }
    ```
    - Create final deliverable
    - Clean up worktrees
    - Terminate monitor agent

11. **Agent cleanup protocol**
    Each agent MUST before posting COMPLETE:
    - Ensure NO uncommitted changes: `git status --porcelain` must be empty
    - If dirty, commit everything: `git add -A && git commit -m "Final: [summary]"`
    - Push to their branch: `git push origin agent/[team-id]/[agent-name]`
    - Verify clean state: `git status` shows "nothing to commit, working tree clean"
    - Post worktree status in COMPLETE message data:
      ```json
      {
        "type": "COMPLETE",
        "data": {
          "branch": "agent/team-id/agent-name",
          "commits": 5,
          "files_changed": 12,
          "worktree_clean": true,
          "final_commit": "abc123def",
          "verified_clean": "git status shows clean"
        }
      }
      ```
    - CRITICAL: Agents with dirty worktrees will be marked as FAILED

## Communication Protocol Reference

### Message Format
```json
{
  "timestamp": "2024-03-19T10:30:45.123Z",
  "agent": "clever-fox-analyzer",
  "type": "STATUS|PROGRESS|COMPLETE|HANDOFF|HANDOFF_ACCEPTED|BLOCKER|BLOCKER_RESOLVED|DISCOVERY|FYI|CRITIQUE",
  "message": "Human readable message",
  "data": {
    "target_agent": "for HANDOFF",
    "source_agent": "for HANDOFF_ACCEPTED",
    "blocker_id": "for BLOCKER/BLOCKER_RESOLVED",
    "details": "any additional structured data"
  }
}
```

### Message Types

**Lifecycle Messages**
- `STATUS`: Current state (required every 5 min) - "Working on X" / "Idle" / "Waiting for Y"
- `PROGRESS`: Completed subtask - "Implemented authentication module"
- `COMPLETE`: Task finished - "All tests passing, PR ready"

**Coordination Messages**
- `HANDOFF`: Request another agent - "Need security-reviewer to check auth implementation"
- `HANDOFF_ACCEPTED`: Accept handoff - "Accepted security review task from implementer"
- `BLOCKER`: Cannot proceed - "Missing API credentials"
- `BLOCKER_RESOLVED`: Blocker cleared - "Obtained credentials from user"

**Information Messages**
- `DISCOVERY`: Critical info - "Found undocumented API endpoint /v2/hidden"
- `FYI`: General info - "Code uses deprecated pattern but still works"
- `CRITIQUE`: Feedback - "Auth implementation missing rate limiting"

### Agent States
- **Active**: Posted STATUS within 5 minutes
- **Idle**: Waiting for input/handoff
- **Blocked**: Has unresolved BLOCKER
- **Complete**: Posted COMPLETE message
- **Dead**: No STATUS for >5 minutes

### Handoff Protocol
1. Agent A posts: `HANDOFF` with `target_agent: "agent-b"`
2. Agent B reads and posts: `HANDOFF_ACCEPTED` with `source_agent: "agent-a"`
3. Agent A can now go idle or work on other tasks
4. Agent B executes handed-off work
5. Agent B posts results and may hand back or complete

### CLI Tool Usage
```bash
# Append message
echo '{"timestamp":"2024-03-19T10:30:45.123Z","agent":"me","type":"STATUS","message":"Working"}' >> channel.jsonl

# Read recent messages
tail -20 channel.jsonl | jq .

# Filter by type
grep '"type":"BLOCKER"' channel.jsonl | jq .

# Count agent messages
grep '"agent":"clever-fox-analyzer"' channel.jsonl | wc -l
```

## Error Handling

- Monitor agent handles stuck detection
- Agents must resolve their own BLOCKERs
- Handoffs must be accepted within 10 minutes
- Critical failures should post BLOCKER then exit

## Desktop Notifications

Monitor agent sends notifications via:
```bash
notify-send "Team Status" "Message here" -u normal
```

Notification schedule:
- Every 5 minutes: Progress summary
- On BLOCKER: Immediate alert
- On all COMPLETE: Final summary
- On timeout: Abort warning

## Team Management

- **Create team**: Use `/spawn-team "task description"` to set up infrastructure
- **Check status**: Use `/team-status` to view all active teams
- **Clean up teams**: Use `/cleanup-team team-id` to remove team completely
- **Team data location**: `~/.claude/teams/{team-id}/`
- **Worktree location**: `~/.claude/worktrees/{team-id}/`

## Git Branch & Worktree Management

### Branch Naming Convention
- **Team branch**: `team/{team-id}` (e.g., `team/clever-fox-20240319-1030`)
- **Agent branches**: `agent/{team-id}/{agent-name}` (e.g., `agent/clever-fox-20240319-1030/analyzer`)
- **All branches** created from current HEAD with dirty state preserved

### Worktree Structure
```
~/.claude/worktrees/
└── {team-id}/
    ├── {agent-1-name}/     # Worktree for agent 1
    ├── {agent-2-name}/     # Worktree for agent 2
    ├── {agent-3-name}/     # Worktree for agent 3
    └── merger/             # Final merge worktree
```

### Collaborative Workflow
1. **Agents work in isolation**: Each in their own worktree/branch
2. **Periodic sync**: Agents pull/merge from team branch
3. **Share stable progress**: Push to team branch when appropriate
4. **Final integration**: Merger agent combines all work
5. **Clean handoff**: All agents must have clean worktrees before COMPLETE

### Worktree Cleanup
- Worktrees removed after successful merge
- Failed teams keep worktrees for debugging
- Use `git worktree prune` after manual cleanup
