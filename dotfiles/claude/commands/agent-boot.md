# Agent Boot

You've been spawned as part of a multi-agent team. Here's how to get started.

## CRITICAL: Do NOT generate your own agent name!

## First Check: Are You Actually a Team Agent?

**BEFORE DOING ANYTHING**, verify you were properly initialized:
1. Check the start of this conversation for `/agent-boot TEAM_ID AGENT_NAME`
2. If you do NOT see that command → **STOP IMMEDIATELY**
3. If you found team branches/directories by accident → **EXIT NOW**
   - Message: "Found team infrastructure but was not initialized as team member via /agent-boot"
   - Do NOT explore team directories without proper initialization

**Only continue if** you have explicit team assignment via `/agent-boot`.

## Instructions

Given parameters like `/agent-boot swift-lion-20240319-1030 analyzer`, this means:
- You're part of team: `swift-lion-20240319-1030`
- Your agent role is: `analyzer`

**First step - get your configuration (THIS TELLS YOU YOUR NAME):**
```bash
ai-teams agent-config swift-lion-20240319-1030 analyzer
```

This tool will tell you:
- **Your identity** (e.g., "swift-lion-20240319-1030-analyzer") - USE THIS AS YOUR NAME
- Your worktree location
- Your Git branch
- How to send status updates
- Where to find the team communication channel
- All other details you need

**IMPORTANT**: The "Your identity" line shows your full agent name. Use this exact name in all communications and commits. Do NOT run generate-agent-name!

## General Architecture

You're part of a multi-agent workflow where:
- Each agent has an isolated Git worktree (no stepping on toes)
- Communication happens via append-only JSONL channel
- You must post STATUS messages every 5 minutes
- Work is integrated via Git branches

## Key Points

1. **Always work in your assigned worktree** - the config will tell you where
2. **Send regular updates** - the config will show you how
3. **Check channel for context** - read what other agents have done
4. **Clean worktree before COMPLETE** - no uncommitted changes

## Git Workflow

**IMPORTANT**: Your `ai-teams agent-config` output shows the Git workflow:
1. Work in your branch (e.g., `agent/team-id/analyzer`)
2. Pull team updates regularly: `git pull origin ai-team/team-id/master`
3. **Push stable work to team branch** (like GitHub master):
   - After completing & testing a feature
   - When code is working and won't break others
   - `git push origin your-branch:ai-team/team-id/master`
4. Final push to your branch: `git push origin your-branch`

The team branch (`ai-team/team-id/master`) is the shared "master" - treat it with respect:
- ✅ Push working, tested code
- ✅ Pull frequently to stay in sync
- ❌ Never push broken code
- ❌ Don't force push

When to push to team branch:
- After implementing & testing a complete feature
- When fixing a bug that others might encounter
- After creating utilities others can use
- When you have stable progress to share

## Communication Checkpoints

**CRITICAL**: Run `ai-teams agent-config` or `ai-teams send` at these checkpoints:
- ✅ After completing any subtask
- ✅ After finishing any compilation/build
- ✅ After every git commit
- ✅ Before starting a new major task
- ✅ When encountering any issue
- ✅ Every 5 minutes regardless (STATUS update)

This ensures you see BLOCKERS, HANDOFFS, and other important messages immediately!

## Sending Messages

After running `ai-teams agent-config`, it shows you how to send messages. Example:

```bash
ai-teams send swift-lion-20240319 analyzer STATUS Working on tests
ai-teams send swift-lion-20240319 analyzer PROGRESS Fixed authentication bug
ai-teams send swift-lion-20240319 analyzer BLOCKER Need API credentials
ai-teams send swift-lion-20240319 analyzer COMPLETE All tests passing
ai-teams send swift-lion-20240319 analyzer DIRECT "Can you check line 42?" --to reviewer
```

Message types:
- **STATUS** - Every 5 minutes (required)
- **PROGRESS** - When you complete something
- **COMPLETE** - When done (clean worktree first!)
- **BLOCKER** - When stuck
- **DISCOVERY** - Important findings
- **HANDOFF** - Need another agent (use --to)
- **HANDOFF_ACCEPTED** - MUST send when you receive a HANDOFF to you!
- **DIRECT** - Direct message to specific agent (use --to)

## Handoff Protocol

When you receive a HANDOFF directed to you:
1. Add the task to your TODO list immediately
2. Send HANDOFF_ACCEPTED with the source agent's name
3. Begin working on the handed-off task

Example:
```bash
# You see: "📨 HANDOFF TO YOU - Please review security implementation"
# You MUST respond:
ai-teams send team-id reviewer HANDOFF_ACCEPTED "Taking on security review from implementer"
```

## Getting Help

- Run `ai-teams agent-config` again if you forget your setup
- Check the team channel for what others are doing
- Look for DISCOVERY messages from other agents

## Final Cleanup

**CRITICAL**: Before you finish, ensure all work is preserved:

1. **Check for uncommitted changes:**
   ```bash
   git status
   ```

2. **Commit any remaining work:**
   ```bash
   git add -A
   git commit -m "Final changes from {your-agent-name}"
   ```

3. **Push EVERYTHING to your branch:**
   ```bash
   git push origin HEAD  # Pushes to your agent branch
   ```

4. **Send COMPLETE message:**
   ```bash
   ai-teams send team-id agent-name COMPLETE "All work pushed to branch"
   ```

5. **Delete your worktree to free space:**
   ```bash
   cd ~  # Leave the worktree first
   git worktree remove ~/.ai-teams/worktrees/{team-id}/{agent-name}
   ```

This ensures:
- ✅ No work is lost
- ✅ Team can access all your changes
- ✅ Disk space is cleaned up
- ✅ Clean exit from the team
