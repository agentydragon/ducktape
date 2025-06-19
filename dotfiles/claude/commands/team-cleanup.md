# Clean Up Agent Teams

Remove stale agent team directories and logs.

## Instructions

1. **List teams directory**
   ```bash
   ls -la ~/.claude/teams/ 2>/dev/null || echo "No teams directory found"
   ```

2. **Find stale teams** (older than 24 hours)
   ```bash
   # Find teams older than 1 day
   find ~/.claude/teams -maxdepth 1 -type d -mtime +1 -name "*-*" | while read dir; do
     team=$(basename "$dir")
     # Get last activity from channel log
     if [ -f "$dir/channel.jsonl" ]; then
       last_msg=$(tail -1 "$dir/channel.jsonl" 2>/dev/null | jq -r .timestamp 2>/dev/null || echo "unknown")
       size=$(du -sh "$dir" | cut -f1)
       echo "Stale: $team (last: $last_msg, size: $size)"
     else
       echo "Empty: $team (no channel log)"
     fi
   done
   ```

3. **Show summary**
   - Count total teams
   - Count stale teams (>24h)
   - Count ancient teams (>7d)
   - Total disk usage

4. **Offer cleanup options**
   Ask user to choose:
   - Clean all teams older than 7 days
   - Clean all teams older than 24 hours
   - Clean specific team by ID
   - Keep all (cancel)

5. **Execute cleanup** (based on choice)
   ```bash
   # For teams older than 7 days
   find ~/.claude/teams -maxdepth 1 -type d -mtime +7 -name "*-*" -exec rm -rf {} + 2>/dev/null

   # For teams older than 24 hours
   find ~/.claude/teams -maxdepth 1 -type d -mtime +1 -name "*-*" -exec rm -rf {} + 2>/dev/null

   # For specific team
   rm -rf ~/.claude/teams/${TEAM_ID}
   ```

6. **Verify cleanup**
   ```bash
   # Show remaining teams
   echo "Remaining teams:"
   ls -la ~/.claude/teams/ | grep -E "^d" | grep -v "^\.$" | grep -v "^\.\.$" | wc -l

   # Show disk usage
   du -sh ~/.claude/teams 2>/dev/null || echo "0 (no teams)"
   ```

## Safety Checks

- Never delete teams with active agents (check for recent messages <10 min)
- Always show what will be deleted before confirming
- Keep teams with unresolved BLOCKERs for analysis
- Preserve teams explicitly marked as "keep" in dashboard.json

## Cleanup Criteria

**Safe to delete:**
- Teams older than 7 days
- Teams with all agents COMPLETE
- Teams with explicit "cleanup_safe": true in dashboard

**Keep for analysis:**
- Teams with unresolved BLOCKERs
- Teams that failed/crashed (for debugging)
- Teams younger than 24 hours

## Example Output

```
Found 5 stale teams:
- clever-fox-2024-03-12-1030 (7d old, 2.3MB, all complete)
- brave-turtle-2024-03-15-1530 (4d old, 856KB, 2 blockers)
- keen-owl-2024-03-18-0900 (1d old, 124KB, abandoned)

Cleanup options:
1. Delete ancient teams (>7 days) - would remove 1 team
2. Delete stale teams (>24 hours) - would remove 3 teams
3. Delete specific team by ID
4. Keep all teams

Choose option (1-4):
```
