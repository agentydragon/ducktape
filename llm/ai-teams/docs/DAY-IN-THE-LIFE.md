# A Day in the Life of an Agent Swarm

## Scenario: Building a REST API with Authentication

**Task**: "Build a REST API for todo management with JWT authentication, comprehensive tests, and documentation"

**Time**: 10:00 AM

### 10:00 - Orchestrator Spawns Team

```bash
/spawn "Build a REST API for todo management with JWT authentication, comprehensive tests, and documentation"
```

Orchestrator (clever-fox) analyzes task and identifies needed agents:
- **designer**: API design and architecture
- **auth-expert**: Authentication implementation
- **api-dev**: Core API implementation
- **tester**: Test suite development
- **documenter**: API documentation
- **reviewer**: Security and code review

```bash
TEAM_ID=$(ai-teams create-team "Build REST API with auth, tests, docs")
# Output: wise-owl-20240319-1000
```

### 10:01 - Monitor Agent Starts

```
Task: Monitor wise-owl-20240319-1000 progress
```

Monitor immediately sends:
```bash
ai-teams send wise-owl-20240319 monitor STATUS "Beginning team progress monitoring"
```

### 10:02 - Designer Agent Boots

```
/agent-boot wise-owl-20240319 designer
```

Designer runs:
```bash
ai-teams agent-config wise-owl-20240319 designer
```

Sees:
```
🕐 Current time: 2024-03-19T10:02:15
📬 0 new message(s) since your last command
🔧 Setting up your worktree at /home/user/.ai-teams/worktrees/wise-owl-20240319/designer...
✅ Pre-commit hooks installed
✅ Worktree created successfully!

📋 Your Configuration:
Your identity: wise-owl-20240319-designer
Your worktree: /home/user/.ai-teams/worktrees/wise-owl-20240319/designer
...
```

Designer sends first status:
```bash
cd /home/user/.ai-teams/worktrees/wise-owl-20240319/designer
ai-teams send wise-owl-20240319 designer STATUS "Starting API design and architecture planning"
```

### 10:03 - Other Agents Boot in Parallel

Auth-expert, api-dev, tester, documenter, and reviewer all boot similarly.

### 10:05 - Designer Makes Discovery

Designer creates initial API spec and finds issue:
```bash
ai-teams send wise-owl-20240319 designer DISCOVERY "Need to decide: REST or GraphQL? REST seems better for todo app simplicity"
ai-teams send wise-owl-20240319 designer PROGRESS "Created initial OpenAPI spec in docs/api-spec.yaml"
git add docs/api-spec.yaml
git commit -m "Initial API specification"
git push origin designer-branch:team/wise-owl-20240319
```

### 10:07 - Auth Expert Sees Discovery

Auth expert runs status update:
```bash
ai-teams send wise-owl-20240319 auth-expert STATUS "Researching JWT implementation options"
```

Sees:
```
📬 2 new message(s) since your last command:
💡 [2024-03-19 10:05:31] wise-owl-20240319-designer: DISCOVERY - Need to decide: REST or GraphQL? REST seems better for todo app simplicity
   [2024-03-19 10:05:45] wise-owl-20240319-designer: PROGRESS - Created initial OpenAPI spec in docs/api-spec.yaml
```

Auth expert pulls team updates:
```bash
git pull origin team/wise-owl-20240319
# Gets the API spec file
```

### 10:10 - First Handoff

Designer completes initial design:
```bash
ai-teams send wise-owl-20240319 designer HANDOFF "API spec ready for implementation. Please implement auth endpoints first." --to auth-expert
ai-teams send wise-owl-20240319 designer STATUS "Idle, available for questions about API design"
```

### 10:11 - Handoff Acknowledgment

Auth expert's next command shows:
```
📨 [2024-03-19 10:10:22] wise-owl-20240319-designer: HANDOFF TO YOU - API spec ready for implementation. Please implement auth endpoints first.
    ⚠️  ACTION REQUIRED: Send HANDOFF_ACCEPTED to acknowledge!
```

Auth expert responds:
```bash
ai-teams send wise-owl-20240319 auth-expert HANDOFF_ACCEPTED "Taking on auth implementation from designer"
ai-teams send wise-owl-20240319 auth-expert STATUS "Implementing JWT auth endpoints"
```

### 10:15 - Blocker Encountered

API dev tries to start but hits issue:
```bash
ai-teams send wise-owl-20240319 api-dev BLOCKER "Cannot start API implementation - need auth middleware interface defined first"
```

### 10:16 - Monitor Notices Pattern

Monitor agent (checking every 5 min) sends notification:
```
notify-send "Team wise-owl: 1 blocker active (api-dev waiting for auth)"
```

### 10:20 - Collaboration via Direct Message

Auth expert sees blocker and responds:
```bash
ai-teams send wise-owl-20240319 auth-expert DIRECT "I'll have auth middleware interface ready in ~10 min" --to api-dev
ai-teams send wise-owl-20240319 auth-expert PROGRESS "Defined auth middleware interface in src/auth/middleware.ts"
git add src/auth/middleware.ts
git commit -m "Auth middleware interface for API integration"
git push origin auth-expert-branch:team/wise-owl-20240319
```

### 10:25 - Blocker Resolved

API dev pulls updates and continues:
```bash
git pull origin team/wise-owl-20240319
ai-teams send wise-owl-20240319 api-dev BLOCKER_RESOLVED "Got auth middleware interface, proceeding with API implementation"
ai-teams send wise-owl-20240319 api-dev STATUS "Implementing todo CRUD endpoints"
```

### 10:30 - Parallel Progress

Multiple agents working simultaneously:
- auth-expert: Implementing JWT logic
- api-dev: Building CRUD endpoints
- tester: Setting up test framework
- documenter: Creating README structure

### 10:45 - Tester Needs Examples

```bash
ai-teams send wise-owl-20240319 tester HANDOFF "Need working auth endpoint to test against" --to auth-expert
```

Auth expert busy, but sends:
```bash
ai-teams send wise-owl-20240319 auth-expert DIRECT "Still implementing, but you can use mock auth for now - see src/auth/mock.ts" --to tester
git add src/auth/mock.ts
git commit -m "Mock auth for testing"
git push origin auth-expert-branch:team/wise-owl-20240319
```

### 11:00 - First Integration

Auth expert completes:
```bash
npm test  # All auth tests pass
git add -A
git commit -m "Complete JWT authentication implementation"
git push origin auth-expert-branch
ai-teams send wise-owl-20240319 auth-expert COMPLETE "JWT auth complete with refresh tokens, tests passing"
```

### 11:30 - Chain of Completions

- api-dev completes CRUD endpoints
- tester completes test suite
- documenter completes API docs

### 11:35 - Reviewer Begins

```bash
ai-teams send wise-owl-20240319 reviewer STATUS "Beginning security and code review"
git pull origin team/wise-owl-20240319
```

Reviewer finds issue:
```bash
ai-teams send wise-owl-20240319 reviewer CRITIQUE "Auth implementation missing rate limiting - potential DoS vector"
ai-teams send wise-owl-20240319 reviewer HANDOFF "Please add rate limiting to auth endpoints" --to auth-expert
```

### 11:36 - Problem: Auth Expert Already Complete!

System detects issue:
```
⚠️  Message Sequencing Warnings:
⚠️  Sending messages after COMPLETE! Did you forget you're done?
```

### 11:37 - Orchestrator Intervenes

Orchestrator sees the critique and spawns new agent:
```
Task: wise-owl-20240319-auth-fix: Fix rate limiting issue
```

### 11:45 - All Issues Resolved

All agents report COMPLETE with clean worktrees.

### 11:46 - Integration Agent Spawned

```
Task: wise-owl-20240319-integrator: Merge all branches and create final deliverable
```

Integrator:
1. Pulls all agent branches
2. Merges into team branch
3. Resolves minor conflicts
4. Runs full test suite
5. Merges team branch to main
6. Creates final summary

### 12:00 - Team Complete

Monitor agent sees all COMPLETE and exits.
Orchestrator reviews final deliverable.

## Friction Points Identified

1. **Handoff after COMPLETE**: Agents can't accept work after completing
   - *Solution*: Orchestrator must spawn new agents for post-complete fixes

2. **Blocker visibility**: Blocked agents might not be noticed quickly
   - *Solution*: Monitor actively flags blockers in notifications

3. **Direct message visibility**: Agents might miss direct messages if not running commands
   - *Solution*: Checkpoints ensure regular command execution

4. **Race conditions**: Multiple agents might try to solve same blocker
   - *Solution*: Clear ownership via handoffs

5. **Stale STATUS**: Easy to forget 5-minute STATUS requirement
   - *Solution*: Warnings remind agents

## Success Factors

1. **Automatic worktree setup**: No manual Git configuration needed
2. **Update stream**: Agents see all relevant messages automatically
3. **Team branch**: Shared integration point prevents conflicts
4. **Pre-commit hooks**: Maintain code quality automatically
5. **Clear protocols**: Handoff acceptance prevents dropped work
