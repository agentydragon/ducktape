# View Team Channel

Display communication log for an agent team.

## Instructions

1. **Get team ID**
   - Accept as parameter or
   - List available teams and let user choose

2. **Basic channel view**
   ```bash
   # Show last 20 messages with formatting
   tail -20 ~/.claude/teams/${TEAM_ID}/channel.jsonl | \
     jq -r '[.timestamp, .agent, .type, .message] | @tsv' | \
     column -t -s $'\t'
   ```

3. **Offer view options**
   - All messages (with pagination)
   - Filter by agent
   - Filter by message type
   - Filter by time range
   - Show only blockers
   - Show only discoveries
   - Follow mode (like tail -f)

4. **Type-specific views**

   **Blockers:**
   ```bash
   grep '"type":"BLOCKER"' ~/.claude/teams/${TEAM_ID}/channel.jsonl | \
     jq -r '[.timestamp, .agent, .message] | @tsv'
   ```

   **Handoffs:**
   ```bash
   grep -E '"type":"HANDOFF|HANDOFF_ACCEPTED"' ~/.claude/teams/${TEAM_ID}/channel.jsonl | \
     jq -r '[.timestamp, .agent, .type, .data.target_agent // .data.source_agent, .message] | @tsv'
   ```

   **Progress timeline:**
   ```bash
   grep -E '"type":"PROGRESS|COMPLETE"' ~/.claude/teams/${TEAM_ID}/channel.jsonl | \
     jq -r '[.timestamp, .agent, .message] | @tsv'
   ```

5. **Agent activity summary**
   ```bash
   # Message count by agent
   jq -r .agent ~/.claude/teams/${TEAM_ID}/channel.jsonl | \
     sort | uniq -c | sort -nr

   # Last message per agent
   jq -r '[.agent, .timestamp, .type] | @tsv' ~/.claude/teams/${TEAM_ID}/channel.jsonl | \
     sort -k1,1 -k2,2r | awk '!seen[$1]++'
   ```

6. **Follow mode** (real-time updates)
   ```bash
   tail -f ~/.claude/teams/${TEAM_ID}/channel.jsonl | \
     jq -r '[.timestamp, .agent, .type, .message] | @tsv'
   ```

## Display Formatting

Use color coding for message types:
- STATUS: default
- PROGRESS: green
- COMPLETE: blue
- BLOCKER: red
- HANDOFF: yellow
- DISCOVERY: magenta
- CRITIQUE: cyan
- ABORT: red bold

## Example Output

```
2024-03-19T10:30:00Z  orchestrator       STATUS    Starting multi-agent workflow
2024-03-19T10:30:15Z  analyzer-api       STATUS    Beginning API analysis
2024-03-19T10:30:45Z  analyzer-api       DISCOVERY Found 3 undocumented endpoints
2024-03-19T10:31:00Z  analyzer-api       HANDOFF   Need security review
2024-03-19T10:31:10Z  security-checker   HANDOFF_ACCEPTED  Taking security review
2024-03-19T10:32:00Z  security-checker   BLOCKER   Missing auth tokens for testing
```
