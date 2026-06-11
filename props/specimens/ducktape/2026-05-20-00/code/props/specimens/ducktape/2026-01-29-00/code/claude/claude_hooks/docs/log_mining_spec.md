# Claude Log Mining Specification

## Overview

Simple approach to mine Claude Code conversation logs and improve hooks using LLM analysis.

## Basic Approach

Load logs from `~/.claude` (well-known JSON format), feed them to an LLM along with current hooks, and ask:

> "Given these conversation patterns, do you see cases where we could have helped Claude by adding a hook, automatic reminder, or autofix? Here's how Claude hooks work: [docs]. Here's our current hooks: [code]."

Let the LLM do the thinking instead of building a complex pipeline.

## What We're Looking For

1. **User Interruptions**: When user stopped Claude mid-action
2. **Corrections**: User feedback after Claude did something wrong
3. **Patterns**: Recurring issues that could be prevented

## Implementation

- Load conversations from `~/.claude/*.json`
- Pass logs + current hook code to LLM
- Get suggestions for new/improved hooks
- Implement promising suggestions

Simple, direct, effective.
