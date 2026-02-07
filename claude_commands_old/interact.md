---
description: Universal interactive mode for complex multi-step tasks
name: interact
---

Enter interactive mode for any complex task, with step-by-step guidance and decision points.

## Usage

```
/interact <task>
/interact(<command>)
```

Examples:

```
U: /interact plan my day
A: Entering interactive planning mode. Let's work through this step by step.

U: /interact(commit)
A: Starting interactive commit mode. I'll guide you through organizing your changes.

U: /interact debug this error
A: Interactive debugging mode. We'll isolate the issue together.
```

## Core Principles

1. **Break complex tasks into steps**
2. **Present options at each decision point**
3. **Allow backtracking and state saves**
4. **Maintain context throughout session**
5. **Exit cleanly with summary**

## Universal Pattern

```
U: /interact <any complex task>
A: 📋 Interactive mode: <task description>

   Let me break this down into steps:
   1. <First major step>
   2. <Second major step>
   3. <Third major step>

   Ready to start with step 1? [yes/skip/customize]
```

## Examples

### Planning Tasks

```
U: /interact plan my day
A: 📋 Interactive mode: Daily Planning

   Let's organize your day. I'll help you:
   1. Review commitments and deadlines
   2. Prioritize tasks
   3. Schedule time blocks
   4. Set up reminders

   First, what commitments do you have today? (meetings/deadlines/none)

U: 2 meetings and a project deadline
A: Let me capture those:

   Meeting 1 - Time and topic?
U: 10am team standup
A: ✓ Added: 10am team standup

   Meeting 2 - Time and topic?
U: 2pm client review
A: ✓ Added: 2pm client review

   Project deadline - What project and when?
U: Auth service PR by 5pm
A: ✓ Added: Auth service PR deadline at 5pm

   Your fixed commitments:
   - 10:00 AM: Team standup
   - 2:00 PM: Client review
   - 5:00 PM: Auth service PR due

   Now let's add your tasks. What else needs to get done? [add tasks/skip to scheduling]
```

### Debugging Sessions

```
U: /interact debug why tests are failing
A: 📋 Interactive mode: Test Debugging

   I'll help you systematically debug the failing tests:
   1. Identify which tests are failing
   2. Examine failure patterns
   3. Isolate root causes
   4. Test fixes

   First, let's see the test results. Run: pytest -v
   [Waiting for output...]

U: [pastes test output showing 5 failures]
A: I see 5 test failures. They seem to fall into 2 categories:

   🔴 Auth-related (3 tests):
   - test_login_valid_credentials
   - test_token_refresh
   - test_logout

   🔴 Database-related (2 tests):
   - test_user_creation
   - test_user_update

   Which group should we investigate first? [auth/database/most recent]

U: auth
A: Looking at the auth test failures, I notice they all have "Connection refused" errors.

   This suggests the auth service might not be running. Let's check:
   1. Is the auth service running? Run: docker ps | grep auth
   2. Check logs: docker logs auth-service

   What do you see?
```

### Code Review Preparation

```
U: /interact prepare code for review
A: 📋 Interactive mode: Code Review Preparation

   I'll help you prepare your code for review:
   1. Check for common issues
   2. Ensure tests pass
   3. Update documentation
   4. Create clear PR description

   Starting with code quality checks...

   🔍 Running linters:
   ✓ Black formatting: OK
   ⚠️ Flake8: 3 warnings
   ✓ Type checking: OK

   Want to see the flake8 warnings? [yes/skip/fix automatically]

U: yes
A: Flake8 warnings:
   1. auth.py:45: W291 trailing whitespace
   2. auth.py:67: E501 line too long (92 > 88 characters)
   3. models.py:23: F401 'datetime' imported but unused

   How would you like to handle these?
   - fix: I'll fix them for you
   - manual: You fix them, tell me when done
   - ignore: Add to PR anyway
```

### Learning New Concepts

```
U: /interact learn kubernetes
A: 📋 Interactive mode: Learning Kubernetes

   Let's create a personalized learning path:
   1. Assess your current knowledge
   2. Set learning goals
   3. Create hands-on exercises
   4. Track progress

   First, what's your experience with:
   - Docker/containers? [none/basic/intermediate/advanced]

U: intermediate
A: Great! You understand containers.

   - Cloud platforms (AWS/GCP/Azure)? [none/basic/intermediate/advanced]

U: basic AWS
A: ✓ Good foundation with containers and AWS basics

   What's your main goal with Kubernetes?
   - Deploy an app
   - Manage infrastructure
   - Pass certification
   - General knowledge

U: deploy an app
A: Perfect! Let's focus on practical deployment skills.

   I'll create a learning path:

   Week 1: Core Concepts
   - Pods, Services, Deployments
   - kubectl basics
   - Local k8s with minikube

   Week 2: Application Deployment
   - Containerize your app
   - Write Kubernetes manifests
   - Deploy to cluster

   Week 3: Production Concerns
   - ConfigMaps & Secrets
   - Health checks
   - Basic monitoring

   Ready to start with Week 1? I'll guide you through setting up minikube first.
```

## Special Features

### State Management

- Save progress: "save state as <name>"
- Restore: "go back to <name>"
- List saves: "show saved states"

### Context Switching

```
U: actually, let me handle the auth tests first
A: 📌 Saving current state: "debugging_database_tests"

   Switching focus to auth tests. You can return with "restore debugging_database_tests"
```

### Progressive Disclosure

```
A: Found 15 issues to address. Showing first 5:
   [list of 5 issues]

   Handle these first? [yes/see all/filter]
```

## Integration with Other Commands

- `/interact(commit)` - Structured commit workflow
- `/interact(cleanup)` - Guided cleanup decisions
- `/interact(review)` - Code review preparation
- Any command can be made interactive

## Best Practices

1. **Start broad, get specific** - Overview first, then drill down
2. **Checkpoint frequently** - Save state at major decision points
3. **Show progress** - "Step 3 of 5 complete"
4. **Allow exits** - "Type 'done' to exit with summary"
5. **Summarize on exit** - What was accomplished

## Exit Handling

```
U: done
A: 📊 Interactive session summary:

   ✅ Completed:
   - Fixed 3 auth test failures
   - Updated test fixtures
   - Added retry logic

   ⏸️ Remaining:
   - 2 database tests still failing
   - Saved as "database_test_debugging"

   Resume with: /interact restore database_test_debugging
```
