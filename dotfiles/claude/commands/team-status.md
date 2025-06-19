# Check Team Status

View the current status of all agent teams.

## Instructions

1. **List all active teams**
   ```bash
   ls -la ~/.claude/teams/ | grep -E "^d" | awk '{print $9}' | grep -v "^\.$" | grep -v "^\.\.$"
   ```

2. **For each team directory found:**
   - Extract team ID from directory name
   - Check if channel.jsonl exists
   - Count total messages in channel
   - Find last message timestamp
   - Count agents by unique agent names
   - Count messages by type

3. **Read dashboard.json if exists**
   ```bash
   if [ -f ~/.claude/teams/${TEAM_ID}/dashboard.json ]; then
     cat ~/.claude/teams/${TEAM_ID}/dashboard.json | jq .
   fi
   ```

4. **Check Git worktrees**
   ```bash
   # List worktrees for this team
   WORKTREE_DIR="$HOME/.claude/worktrees/${TEAM_ID}"
   if [ -d "$WORKTREE_DIR" ]; then
     echo "=== Worktrees ==="
     for agent_dir in "$WORKTREE_DIR"/*; do
       if [ -d "$agent_dir" ]; then
         agent=$(basename "$agent_dir")
         cd "$agent_dir" 2>/dev/null && {
           branch=$(git branch --show-current)
           status=$(git status --porcelain | wc -l)
           if [ $status -eq 0 ]; then
             echo "✓ $agent: $branch (clean)"
           else
             echo "⚠ $agent: $branch (dirty: $status uncommitted changes)"
           fi
         }
       fi
     done
     cd - >/dev/null
   fi
   ```

5. **Analyze channel activity**
   ```bash
   # Get last 10 messages
   tail -10 ~/.claude/teams/${TEAM_ID}/channel.jsonl | jq -r '[.timestamp, .agent, .type, .message] | @tsv'

   # Count active agents (posted in last 10 minutes)
   CUTOFF=$(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%S)
   grep -E "\"timestamp\":\"2" ~/.claude/teams/${TEAM_ID}/channel.jsonl | \
     jq -r 'select(.timestamp > "'$CUTOFF'") | .agent' | sort -u | wc -l

   # Find blockers
   grep '"type":"BLOCKER"' ~/.claude/teams/${TEAM_ID}/channel.jsonl | \
     grep -v '"type":"BLOCKER_RESOLVED"' | jq .
   ```

6. **Generate summary report**
   For each team, report:
   - Team ID and age
   - Total agents involved
   - Active/idle/blocked/complete counts
   - Worktree status (clean/dirty count)
   - Current blockers if any
   - Last activity time
   - Overall progress estimate

7. **Identify stale teams**
   ```bash
   # Check directory age
   find ~/.claude/teams -maxdepth 1 -type d -mtime +1 -printf "%T@ %p\n" | \
     sort -n | awk '{print $2}' | xargs -I{} basename {} | \
     while read team; do
       if [ -f ~/.claude/teams/$team/channel.jsonl ]; then
         LAST_MSG=$(tail -1 ~/.claude/teams/$team/channel.jsonl | jq -r .timestamp 2>/dev/null || echo "")
         if [ -n "$LAST_MSG" ]; then
           echo "Stale team: $team (last activity: $LAST_MSG)"
         fi
       fi
     done
   ```

7. **Highlight concerns**
   - Teams with no activity >10 minutes (possibly stuck)
   - Teams with all agents blocked
   - Teams running >2 hours
   - Unresolved blockers >30 minutes old
   - Stale teams (>24 hours old)

8. **Suggest cleanup**
   If stale teams found:
   - List team IDs and ages
   - Mention `/team-cleanup` command for removal
   - Show disk usage: `du -sh ~/.claude/teams/*`
