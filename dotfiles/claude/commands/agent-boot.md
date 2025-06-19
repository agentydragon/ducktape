# Agent Boot

You've been spawned as part of a multi-agent team. Here's how to get started.

## Instructions

Given parameters like `/agent-boot swift-lion-20240319-1030 analyzer`, this means:
- You're part of team: `swift-lion-20240319-1030`
- Your agent role is: `analyzer`

**First step - get your configuration:**
```bash
ai-teams agent-config swift-lion-20240319-1030 analyzer
```

This tool will tell you:
- Your full agent name
- Your worktree location
- Your Git branch
- How to send status updates
- Where to find the team communication channel
- All other details you need

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

## Sending Messages

After running `ai-teams agent-config`, it shows you how to send messages. Example:

```bash
ai-teams send swift-lion-20240319 analyzer STATUS Working on tests
ai-teams send swift-lion-20240319 analyzer PROGRESS Fixed authentication bug
ai-teams send swift-lion-20240319 analyzer BLOCKER Need API credentials
ai-teams send swift-lion-20240319 analyzer COMPLETE All tests passing
```

Message types:
- **STATUS** - Every 5 minutes (required)
- **PROGRESS** - When you complete something
- **COMPLETE** - When done (clean worktree first!)
- **BLOCKER** - When stuck
- **DISCOVERY** - Important findings
- **HANDOFF** - Need another agent

## Getting Help

- Run `ai-teams agent-config` again if you forget your setup
- Check the team channel for what others are doing
- Look for DISCOVERY messages from other agents
