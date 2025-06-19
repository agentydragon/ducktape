# Spawn Agents for Team

Spawn all agents for a previously created team.

## Instructions

1. **Get team ID from parameter**
   ```bash
   TEAM_ID="$1"
   if [ -z "$TEAM_ID" ]; then
     echo "❌ Error: Team ID required"
     echo "Usage: /spawn-agents <team-id>"
     exit 1
   fi
   ```

2. **Load team information**
   ```bash
   TEAM_DIR="$HOME/.claude/teams/$TEAM_ID"
   if [ ! -f "$TEAM_DIR/spawn-info.json" ]; then
     echo "❌ Error: Team $TEAM_ID not found or not properly initialized"
     exit 1
   fi

   # Parse spawn info
   ORCHESTRATOR=$(jq -r .orchestrator "$TEAM_DIR/spawn-info.json")
   AGENT_COUNT=$(jq -r '.agents | length' "$TEAM_DIR/spawn-info.json")
   ```

3. **Spawn monitor agent**
   Use Task tool:
   ```
   Task: {
     description: "${ORCHESTRATOR}-monitor: Team progress monitor",
     prompt: "Monitor $TEAM_DIR/channel.jsonl. Every 5 minutes: 1) Check all agents reported within 5 min, 2) Summarize progress since last notification, 3) Send desktop notification with: 'Team $TEAM_ID: X/Y agents active, Z complete. Current: [one-line summary]'. Exit conditions: a) No new entries for 10 min - send ABORT notification and exit, b) All agents COMPLETE or failed - send final summary and exit, c) All agents have active BLOCKER and no progress for 10 min - send 'All agents blocked' notification and exit. Post your own STATUS messages to the channel."
   }
   ```

4. **Spawn worker agents**
   For each agent in spawn-info.json:
   ```bash
   jq -c '.agents[]' "$TEAM_DIR/spawn-info.json" | while read agent_json; do
     AGENT_NAME=$(echo "$agent_json" | jq -r .name)
     AGENT_ROLE=$(echo "$agent_json" | jq -r .role)
     AGENT_WORKTREE=$(echo "$agent_json" | jq -r .worktree)
     AGENT_BRANCH=$(echo "$agent_json" | jq -r .branch)

     # Read task parameters
     PARAMS_FILE="$TEAM_DIR/task-params-$AGENT_NAME.json"

     # Spawn agent using Task tool
   done
   ```

   For each agent, use Task tool:
   ```
   Task: {
     description: "$AGENT_NAME: $AGENT_ROLE",
     prompt: "Your name is $AGENT_NAME. CRITICAL: cd to your worktree at $AGENT_WORKTREE FIRST. Load task parameters from $PARAMS_FILE and use them with /agent-task command. Follow all communication protocols. Post to channel.jsonl using: echo '{}' >> $TEAM_DIR/channel.jsonl. Work ONLY in your worktree, commit to your branch $AGENT_BRANCH."
   }
   ```

5. **Wait briefly then spawn merger**
   After all agents spawned, wait 30 seconds then:
   ```
   Task: {
     description: "${ORCHESTRATOR}-merger: Final integration",
     prompt: "cd $HOME/.claude/worktrees/$TEAM_ID. Create merger worktree. Monitor $TEAM_DIR/channel.jsonl for all agents to complete. Once all complete, for each agent branch: 1) git merge agent/$TEAM_ID/[agent] into team branch, 2) Resolve any conflicts, 3) Test integrated changes. Finally: merge team branch back to original branch. Post COMPLETE with summary of integrated changes."
   }
   ```

6. **Log spawn completion**
   ```bash
   echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)\",\"agent\":\"orchestrator\",\"type\":\"STATUS\",\"message\":\"All agents spawned\"}" >> "$TEAM_DIR/channel.jsonl"
   ```

## Usage

```
/spawn-agents clever-fox-20240319-1030
```

This will spawn all agents for the specified team.
