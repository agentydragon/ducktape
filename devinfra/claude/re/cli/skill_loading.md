# Claude Code CLI: Skill Loading and Discovery

Reverse-engineered from Claude Code binary v2.1.80 (2026-03-19 build).

## Overview

Claude Code has **three** skill discovery mechanisms:

1. **Initial load** — memoized, on first access per session
2. **Chokidar file watcher** — watches existing `.claude/skills/` directories for changes
3. **Dynamic discovery** — discovers new `.claude/skills/` directories when Claude reads/writes files in subdirectories

## Skill Sources

Skills are loaded from multiple locations in priority order:

| Source           | Description                                  | Settings gate     |
| ---------------- | -------------------------------------------- | ----------------- |
| Managed (policy) | `<project>/.claude/skills/`                  | `policySettings`  |
| User             | `~/.claude/skills/` (inside managed dir)     | `userSettings`    |
| Project          | `<cwd>/.claude/skills/`                      | `projectSettings` |
| Additional dirs  | `<dir>/.claude/skills/` for `--add-dir` dirs | `projectSettings` |
| Plugins          | From installed marketplace plugins           | separate          |
| Bundled          | Built into the CLI binary                    | always            |

## Loading Flow

### 1. Initial Skill Load (Memoized)

Skills are loaded via memoized async functions (`fA()` wrapper = memoize):

- `bUA(cwd)` — loads skills from all skill directories (managed, user, project, additional)
- `I0(cwd)` — combines skill dir commands + plugin skills + bundled skills + dynamic skills
- `Dh(cwd)` — filters to prompt-type skills for the Skill tool listing

These are **lazily evaluated** — not called at startup, but on first access. The first
access happens when the system prompt is assembled for the first API call, specifically
via the `skill_listing` system-reminder delta (`MR6`).

### 2. File Watcher (Chokidar)

**Initialization** (`P$_` / `lkH.initialize`):

- Called from `Zd$()`, the post-render initialization function
- `Zd$()` runs right after the React app mounts (`B7H` calls `render()` then `Zd$()`)
- Runs `w$_()` to discover which directories to watch
- **Only watches directories that already exist** — `fs.stat()` is called on each candidate path, and paths that don't exist are silently skipped

**Watched directories** (`w$_`):

- `~/.claude/skills/` (user settings)
- `~/.claude/commands/` (user commands, deprecated)
- `<cwd>/.claude/skills/` (project settings)
- `<cwd>/.claude/commands/` (project commands, deprecated)
- `<dir>/.claude/skills/` for each `--add-dir` directory

**Watcher config**:

```
persistent: true
ignoreInitial: true
depth: 2
awaitWriteFinish.stabilityThreshold: 1000ms (configurable)
awaitWriteFinish.pollInterval: 500ms (configurable)
reloadDebounce: 300ms (configurable)
chokidarInterval: 2000ms (configurable)
usePolling: true if Bun runtime, false otherwise
atomic: true
```

**On change** (`GsA` → `z$_` → debounced reload):

1. Detected change path is added to a pending set
2. After 300ms debounce, fires a `ConfigChange` hook check
3. If not blocked by hook: clears ALL skill caches (`PC$`, `Ug`, `oaH`)
4. Notifies all subscribers (React components re-render skill listings)
5. Skills are re-fetched from disk on next turn

### 3. Dynamic Skill Discovery (File Operation Triggered)

When Claude's `Read`, `Edit`, or `Write` tools touch files, they call `_TH(filePaths, cwd)`
which walks **up the directory tree** from each touched file looking for `.claude/skills/`
directories that haven't been seen yet.

Example: Claude reads `/home/user/project/subdir/foo.py` →
walks up checking:

- `/home/user/project/subdir/.claude/skills/`
- `/home/user/project/.claude/skills/` (already known, skip)

If new skill directories are found:

1. They're added to `dynamicSkillDirTriggers` on the tool context
2. `MTH(dirs)` loads skills from those directories into `qn` (dynamic skills map)
3. `_R6` (the `dynamic_skill` system-reminder delta) reads `dynamicSkillDirTriggers` on next turn and reports newly found skills to the model

Dynamic skills are loaded into a **separate map** (`qn`) from the main memoized skills.
They're merged in `I0()`: dynamic skills (`qn`) are appended after the main skills,
deduplicated by name.

## Session Start Hook Timing

### Startup Sequence

1. **App mounts** — React render
2. **`Zd$()` runs** — post-render initialization:
   - Initializes various subsystems
   - **`lkH.initialize()`** — starts chokidar watcher for existing `.claude/skills/` dirs
3. **`MI("startup")` is kicked off** — runs session start hooks (async, may run in parallel with MCP init)
4. **First user message processed** — triggers first API call
5. **System prompt assembled** — `skill_listing` delta calls `Dh(cwd)` for first skill load

### Key Ordering Detail

`MI("startup")` (session start hooks) and `s6` (MCP initialization) are started
**concurrently** via `Promise.all([s6, m6])` in the interactive startup path.

The session start hooks produce messages that are prepended to the conversation.
These messages are assembled **before** the first API call, but the skill loading
itself happens later (lazily, when the system prompt needs skill listings).

### What Happens After Session Start Hooks Complete

When a SessionStart hook process completes:

1. `checkForNewResponses` detects `isSessionStart: true`
2. Calls `qUD()` — invalidates the session environment cache (env vars from hook output scripts)
3. Does **NOT** explicitly invalidate skill caches

## Answer: Will a New Skill from Session Start Hook Be Picked Up?

**It depends on the scenario:**

### Scenario A: `.claude/skills/` directory already exists

If the directory exists before Claude starts, chokidar watches it. When the hook writes
a new `SKILL.md` file:

1. Chokidar detects the `add` event (after `awaitWriteFinish` stabilization — ~1 second)
2. After 300ms debounce, clears all skill caches
3. Next turn re-fetches skills from disk — **new skill IS available**

**Result: YES**, the skill will be picked up, with ~1.3s delay.

### Scenario B: `.claude/skills/` directory is created by the hook

If the hook creates the directory itself (it didn't exist before):

1. Chokidar was initialized before the hook ran — it didn't watch this path (stat failed)
2. The chokidar watcher does **NOT** re-scan for new directories after initialization
3. The `WsA` flag prevents re-initialization (set to `true` on first call)
4. Dynamic discovery (`_TH`) only triggers from Read/Edit/Write tool file operations

**Result: NO**, the skill will NOT be automatically picked up. It requires either:

- A file operation (Read/Edit/Write) in a subdirectory that triggers `_TH` to walk up and find it
- A session restart
- Or: create the directory before Claude starts (e.g., in the `environment-manager` bootstrap)

### Scenario C: Skill written to user-level `~/.claude/skills/`

Same analysis as A/B — depends on whether `~/.claude/skills/` existed at watcher init time.
In the web environment, `environment-manager` creates this directory during bootstrap (step 4),
before the Claude CLI starts, so it WILL be watched.

## Relevant Minified Symbol Map

| Minified | Likely original name         | Purpose                                                 |
| -------- | ---------------------------- | ------------------------------------------------------- |
| `bUA`    | `loadSkillDirCommands`       | Loads skills from skill directories (memoized)          |
| `I0`     | `getAllSkills` / `getSkills` | Combines all skill sources (memoized)                   |
| `Dh`     | `getPromptSkills`            | Filters to prompt-type skills for Skill tool (memoized) |
| `PC$`    | `clearSkillDirCache`         | Clears `bUA` cache + dynamic skills                     |
| `oaH`    | `clearAllSkillCaches`        | Clears `I0`, `Dh`, `ZqH` caches                         |
| `Ug`     | `invalidateAllCaches`        | Calls `oaH` + `PC$` + other cache clears                |
| `P$_`    | `initializeWatcher`          | Sets up chokidar on skill/command dirs                  |
| `w$_`    | `getWatchPaths`              | Discovers existing directories to watch                 |
| `GsA`    | `onSkillFileChanged`         | Chokidar change handler                                 |
| `z$_`    | `scheduleSkillReload`        | Debounced reload after file change                      |
| `_TH`    | `discoverSkillDirs`          | Dynamic discovery from file operations                  |
| `MTH`    | `loadDynamicSkills`          | Loads skills from dynamically discovered dirs           |
| `qn`     | `dynamicSkillsMap`           | Map of dynamically discovered skills                    |
| `MR6`    | `skillListingDelta`          | System-reminder delta for skill listings                |
| `_R6`    | `dynamicSkillDelta`          | System-reminder delta for dynamic skill dirs            |
| `MI`     | `runSessionStartHooks`       | Orchestrates SessionStart hook execution                |
| `Zd$`    | `postRenderInit`             | Post-mount initialization (starts watcher)              |
| `lkH`    | `skillWatcher` module        | Watcher module exports (initialize, dispose, subscribe) |
| `WsA`    | `isInitialized`              | Prevents re-initialization of watcher                   |
| `EsA`    | `isDisposed`                 | Tracks watcher disposal                                 |
| `YU`     | `watcher`                    | Chokidar FSWatcher instance                             |
| `GtH`    | `pendingChanges`             | Set of changed paths pending reload                     |
| `QkH`    | `subscribers`                | Set of callback functions for change notifications      |
| `fA`     | `memoize`                    | Memoization wrapper                                     |
| `qUD`    | `invalidateSessionEnvCache`  | Clears cached session environment                       |
