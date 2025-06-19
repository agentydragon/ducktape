# Multi-Agent System States

## Valid Agent States

### 1. **ACTIVE**
- Has sent STATUS within last 5 minutes
- May be working on tasks
- Can send any message type
- Should pull from team branch regularly

### 2. **IDLE**
- Waiting for handoff or input
- Still must send STATUS every 5 minutes
- Can accept handoffs
- Should monitor channel for opportunities

### 3. **BLOCKED**
- Has unresolved BLOCKER message
- Cannot proceed with main task
- Should work on resolving blocker or alternative tasks
- Must update when BLOCKER_RESOLVED

### 4. **COMPLETE**
- Has sent COMPLETE message
- Worktree must be clean (no uncommitted changes)
- Should NOT send more messages (except STATUS if asked)
- Branch pushed and ready for integration

### 5. **DEAD**
- No STATUS for >5 minutes
- Considered failed/crashed
- Monitor agent should flag this

## Valid Team States

### 1. **INITIALIZING**
- Team branch created
- No agents active yet
- Channel exists but mostly empty

### 2. **ACTIVE**
- One or more agents working
- Regular message flow
- Monitor agent running

### 3. **PARTIALLY_COMPLETE**
- Some agents COMPLETE
- Others still working
- Integration not yet started

### 4. **INTEGRATING**
- All worker agents COMPLETE
- Integrator agent merging branches
- Final deliverable being prepared

### 5. **COMPLETE**
- All agents finished
- Team branch merged to original
- Ready for cleanup

## Invalid States to Detect/Prevent

### Agent Level
1. **Dirty worktree with COMPLETE** - Agent claims done but has uncommitted work
2. **Messages after COMPLETE** - Agent continues working after claiming done
3. **No initial STATUS** - Agent starts work without announcing presence
4. **Unacknowledged HANDOFF** - Handoff sent but never accepted
5. **Double HANDOFF acceptance** - Multiple agents accept same handoff
6. **STATUS timeout** - No STATUS for >5 minutes while not COMPLETE

### Team Level
1. **All agents BLOCKED** - Deadlock, no progress possible
2. **No monitor agent** - Team running without progress tracking
3. **Orphaned handoffs** - Handoffs to non-existent agents
4. **Branch conflicts** - Agents pushing incompatible changes to team branch
5. **Incomplete integration** - Some agent branches never merged

## State Transitions

```
INITIALIZING -> ACTIVE (first agent starts)
ACTIVE -> IDLE (waiting for input)
IDLE -> ACTIVE (receives handoff or resumes work)
ACTIVE -> BLOCKED (encounters blocker)
BLOCKED -> ACTIVE (blocker resolved)
ACTIVE -> COMPLETE (finishes all work)
ANY -> DEAD (no STATUS for >5 minutes)
```

## Recovery Procedures

### Agent Crash Recovery
1. Monitor detects DEAD agent
2. Orchestrator spawns replacement agent
3. New agent examines crashed agent's branch
4. Continues or restarts work as appropriate

### Deadlock Recovery
1. Monitor detects all agents BLOCKED
2. Sends notification to orchestrator
3. Orchestrator intervenes to resolve blockers
4. Or spawns specialist agent to unblock

### Integration Failure Recovery
1. Integrator detects merge conflicts
2. Attempts automatic resolution
3. If fails, creates BLOCKER for orchestrator
4. May spawn specialist to resolve conflicts
