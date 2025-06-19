# Abort Agent Team

Emergency stop for a running agent team.

## Instructions

1. **Get team ID from user**
   If not provided, show active teams and ask which to abort

2. **Verify team exists**
   ```bash
   if [ ! -d ~/.claude/teams/${TEAM_ID} ]; then
     echo "Team ${TEAM_ID} not found"
     exit 1
   fi
   ```

3. **Check current status**
   ```bash
   # Count active agents (posted within 5 minutes)
   CUTOFF=$(date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%S)
   ACTIVE=$(grep -E "\"timestamp\":\"2" ~/.claude/teams/${TEAM_ID}/channel.jsonl | \
     jq -r 'select(.timestamp > "'$CUTOFF'") | .agent' | sort -u | wc -l)

   echo "Team ${TEAM_ID} has $ACTIVE active agents"
   ```

4. **Post ABORT message to channel**
   ```bash
   ABORT_MSG=$(cat <<EOF
   {
     "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)",
     "agent": "orchestrator-abort",
     "type": "ABORT",
     "message": "Emergency abort requested by user",
     "data": {"reason": "user_requested"}
   }
   EOF
   )

   echo "$ABORT_MSG" >> ~/.claude/teams/${TEAM_ID}/channel.jsonl
   ```

5. **Update dashboard to aborted status**
   ```bash
   if [ -f ~/.claude/teams/${TEAM_ID}/dashboard.json ]; then
     # Update status to aborted
     jq '.status = "aborted" | .aborted_at = "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"' \
       ~/.claude/teams/${TEAM_ID}/dashboard.json > \
       ~/.claude/teams/${TEAM_ID}/dashboard.json.tmp
     mv ~/.claude/teams/${TEAM_ID}/dashboard.json.tmp \
        ~/.claude/teams/${TEAM_ID}/dashboard.json
   fi
   ```

6. **Send notification**
   ```bash
   notify-send "Team Aborted" "Team ${TEAM_ID} has been aborted" -u critical
   ```

7. **Create abort marker file**
   ```bash
   touch ~/.claude/teams/${TEAM_ID}/ABORTED
   echo "Aborted at $(date)" > ~/.claude/teams/${TEAM_ID}/ABORTED
   ```

8. **Show final status**
   - List all agents that were active
   - Show any unresolved blockers
   - Confirm abort completed
   - Suggest using `/team-cleanup` if needed

## Abort Protocol

When agents see ABORT in channel:
1. Stop current work immediately
2. Post final STATUS with "Aborted" message
3. Exit without cleanup (preserve state for debugging)

## Safety Notes

- Abort doesn't kill processes, just signals them to stop
- Agents should check for ABORT messages periodically
- State is preserved for post-mortem analysis
- Use `/team-cleanup` later to remove aborted team data
