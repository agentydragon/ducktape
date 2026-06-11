# LINT ENFORCER Hook Specification

## Overview

The LINT ENFORCER hook tracks **non-autofixable** lint violations in files that Claude Code touches, provides incremental feedback during editing, and ensures all manual lint issues are resolved before Claude completes a turn. It works in tandem with AUTOFIXER - only focusing on violations that require manual intervention.

## Architecture Principles

**Events**: `PostToolUse` (for tracking) + `Stop` (for enforcement)
**Matchers**: `Edit|MultiEdit|Write` for PostToolUse, no matcher for Stop
**Blocking**: Never blocks on PostToolUse, conditionally blocks on Stop
**Execution Order**: Runs after AUTOFIXER (higher timeout or second hook)

## Core Principle: Manual-Only Violations

**Key Filter**: Only track and enforce violations that are **NOT** autofixable by ruff or other pre-commit tools.

### Violation Classification

**Autofixable (IGNORE these)**:

- Line length, trailing whitespace, import sorting
- Most formatting and whitespace issues
- Unused imports, basic syntax fixes

**Manual Only (TRACK these)**:

- Undefined variables requiring definition
- Unused variables requiring code removal
- Function complexity requiring refactoring
- Naming convention violations requiring renaming
- Logic errors requiring code changes

## Hook Behavior

### PostToolUse (Tracking)

1. **Wait for AUTOFIXER** to complete (run as second hook)
2. **Scan for lint violations** after autofix has run
3. **Filter to manual-only violations** in edited sections
4. **Provide targeted feedback** about issues Claude must fix
5. **Never block** - always allow Claude to continue

### Stop (Enforcement)

When Claude wants to finish:

1. **Re-scan all touched files** (in case violations were introduced later)
2. **Filter to manual violations only** in Claude's edited sections
3. **If manual violations exist**, block with specific fix instructions
4. **If only autofixable violations**, allow stop (AUTOFIXER will handle next edit)

## State Management

Tracks session-specific manual violations with:

- File paths and edited sections
- Violation details (line, rule, message)
- Attribution to Claude's changes
- Timestamps for session management

## Integration Strategy

### Execution Coordination

- AUTOFIXER runs first and fixes what it can
- LINT ENFORCER scans the post-autofix file state
- Only manual violations remain for tracking
- No overlap in responsibilities

### Feedback System

Provides categorized feedback grouped by violation type:

- Undefined variables
- Unused variables
- Code complexity
- Naming conventions
- Logic errors
- Bug-prone patterns

## Configuration

Uses YAML configuration for:

- Manual-only rule enforcement (core principle)
- Custom autofixable rule definitions
- Enforcement behavior settings
- Violation display limits

## Testing Approach

1. **Autofixable detection tests** - verify correct classification
2. **Integration with AUTOFIXER** - ensure proper execution order
3. **Manual violation enforcement** - test blocking behavior
4. **Session state management** - persistence across turns
5. **Real workflow tests** - introduce violations, verify Claude fixes them

This approach ensures Claude learns to write clean code by handling the issues that tools can't automatically fix, while avoiding redundant feedback about formatting issues that are auto-corrected.
