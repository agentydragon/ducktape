# Agent Boot Monitor

Special variant of agent-boot for team monitor agents.

## Instructions

You're the monitor agent for a multi-agent team. Follow the standard agent-boot process, but with these critical additions:

1. **First, execute the standard agent-boot**:
   ```
   /agent-boot [team-id] monitor
   ```

2. **Additional monitor-specific requirements**:

   **Early exit conditions:**
   - If NO other agents appear in channel within 3 minutes → EXIT with error "CRITICAL: No agents spawned - orchestrator stuck on Task blocking"
   - If zero agent activity for 10 minutes → EXIT with error "Team appears abandoned"

   **Monitoring duties:**
   - Check `ai-teams channel [team-id]` every 5 minutes
   - Count active agents (those sending STATUS within last 5 min)
   - Send desktop notifications for important events:
     - First agent joins
     - Any BLOCKER messages
     - When 50% agents COMPLETE
     - When all agents COMPLETE

   **Exit criteria:**
   - All agents report COMPLETE status
   - 15 minutes of inactivity after at least one agent was active
   - Error conditions mentioned above

3. **Critical difference from regular agents**:
   You MUST detect if the orchestrator failed to spawn other agents (common when Task tool is misused).

## Example Timeline

**Failure case (orchestrator stuck):**
- 0 min: Monitor starts, sees only own STATUS
- 3 min: Still no other agents → EXIT ERROR "orchestrator stuck on Task blocking"

**Success case:**
- 0 min: Monitor starts
- 2 min: 5 agents join and start working
- 30 min: 3 agents COMPLETE
- 45 min: All agents COMPLETE → Monitor exits normally

## Usage

Called by orchestrator as:
```
/agent-boot-monitor swift-lion-20240319-1030
```

This ensures the monitor doesn't wait forever if orchestrator gets stuck.
