# Claude Code Hook Environment Resolution

**Source**: Live binary analysis of `/opt/claude-code/bin/claude` v2.1.42
(compiled Bun/Node.js SEA binary, ~228 MB).

## How Hooks Get Their Environment

Claude Code command hooks are spawned via Node.js `child_process.spawn()` with
an explicitly constructed `env` object. The relevant function is `Gd$` in the
minified bundle.

### Environment Construction

```javascript
// subprocessEnv() — provides base environment for all subprocesses
function subprocessEnv() {
  let upstreamProxyEnv = registeredUpstreamProxyEnvFn?.() ?? {};
  if (!process.env.CLAUDE_CODE_SUBPROCESS_ENV_SCRUB)
    return Object.keys(upstreamProxyEnv).length > 0 ? { ...process.env, ...upstreamProxyEnv } : process.env;
  // If scrubbing enabled: clone process.env, merge upstream proxy env,
  // then delete sensitive keys (API keys, tokens, etc.)
  let scrubbed = { ...process.env, ...upstreamProxyEnv };
  for (let key of SCRUBBED_KEYS) {
    delete scrubbed[key];
    delete scrubbed[`INPUT_${key}`];
  }
  return scrubbed;
}
```

For command hooks specifically:

```javascript
let env = {
  ...subprocessEnv(), // process.env (+ upstream proxy overrides)
  CLAUDE_PROJECT_DIR: cwd, // Always set
};

// For plugin hooks:
if (pluginRoot) env.CLAUDE_PLUGIN_ROOT = pluginRoot;
if (pluginId) env.CLAUDE_PLUGIN_DATA = pluginDataDir;
// Plugin options as CLAUDE_PLUGIN_OPTION_<KEY>=<value>

// For skill hooks:
if (skillRoot) env.CLAUDE_PLUGIN_ROOT = skillRoot;

// CLAUDE_ENV_FILE only for: SessionStart, Setup, CwdChanged, FileChanged
if (hookEvent in ["SessionStart", "Setup", "CwdChanged", "FileChanged"])
  env.CLAUDE_ENV_FILE = await createEnvFile(hookEvent, hookIndex);

// Spawn with explicit env
child_process.spawn(command, [], { env, cwd, shell: true });
```

### Key Insight: PATH Comes from `process.env`

The hook subprocess inherits `process.env` from the Claude Code Node.js process.
This is the environment that `environment-manager` set when spawning `claude`.

The `process.env.PATH` at Claude startup is:

```
/root/.local/bin:/root/.cargo/bin:/usr/local/go/bin:/opt/node22/bin:
/opt/maven/bin:/opt/gradle/bin:/opt/rbenv/bin:/root/.bun/bin:
/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```

This PATH is set by the container image and inherited through:
`process_api` → `environment-manager` → `claude` → hook subprocess.

### Shell Snapshot (Bash Tool) is Different

The Bash tool uses a **shell snapshot** mechanism that sources profile scripts:

```javascript
child_process.execFile(shellPath, ["-c", "-l", snapshotScript], {
  env: {
    ...(process.env.CLAUDE_CODE_DONT_INHERIT_ENV ? {} : subprocessEnv()),
    SHELL: shellPath,
    GIT_EDITOR: "true",
    CLAUDECODE: "1",
  },
  ...
});
```

The `-l` flag makes it a **login shell**, which sources `/etc/profile`,
`/etc/profile.d/*.sh`, and `~/.profile`/`~/.bashrc`. So PATH modifications
in profile scripts ARE picked up by the Bash tool but NOT by command hooks.

### `CLAUDE_CODE_SHELL_PREFIX` — Undocumented Hook PATH Override

If the env var `CLAUDE_CODE_SHELL_PREFIX` is set, hook commands are prefixed:

```javascript
let finalCommand = process.env.CLAUDE_CODE_SHELL_PREFIX
  ? applyShellPrefix(process.env.CLAUDE_CODE_SHELL_PREFIX, command)
  : command;
```

This could theoretically be used to inject a `source` or `export PATH=...`
before the hook command, but it's not documented and may change.

### Session Environment Scripts

For `SessionStart`/`Setup`/`CwdChanged`/`FileChanged` hooks, Claude Code
provides a `CLAUDE_ENV_FILE` path. Hooks can write shell exports to this file:

```bash
echo 'export PATH="/my/custom/bin:$PATH"' >> "$CLAUDE_ENV_FILE"
```

These exports are **only** consumed by the Bash tool (via the session env
sourcing chain), NOT by subsequent hook invocations.

The session env script loading path:

```
~/.claude/session-env/<session-id>/
  ├── sessionstart-hook-0.sh   # Written by first SessionStart hook
  ├── sessionstart-hook-1.sh   # Written by second SessionStart hook
  ├── setup-hook-0.sh          # Written by Setup hooks
  └── ...
```

These are sorted and sourced in order before each Bash tool command.

## How to Get Custom PATH into Hooks

### Method 1: Install to Existing PATH (Recommended)

Put binaries in `/root/.local/bin` or `/usr/local/bin` — already on PATH
for both hooks and Bash tool.

### Method 2: Modify `process.env` Before Claude Starts

The setup script (init_script) runs before `environment-manager` launches
Claude. If you can get `environment-manager` to propagate PATH changes to
Claude's process.env, hooks would inherit them. However,
`environment-manager` constructs Claude's env from its own `process.env`,
and the init script runs as a separate child process — env changes don't
propagate back.

The `environment_variables` field in the anthropic config **can** inject
env vars into the session, but it's controlled by the sandbox-gateway API,
not by the user's setup script.

### Method 3: Wrapper Script

Instead of a bare command in your hook config, use a wrapper:

```json
{
  "type": "command",
  "command": "bash -c 'export PATH=/my/bin:$PATH; exec my-hook-binary'"
}
```

This works because hooks are spawned with `shell: true`.

### Method 4: CLAUDE_ENV_FILE (SessionStart Only → Bash Tool Only)

Write PATH exports to `$CLAUDE_ENV_FILE` in a SessionStart hook. This
affects subsequent Bash tool commands but NOT other hook invocations.

## Scrubbed Environment Variables

When `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` is set, these keys are removed
from subprocess environments (hooks and Bash tool):

- `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN`
- `ANTHROPIC_FOUNDRY_API_KEY`, `ANTHROPIC_CUSTOM_HEADERS`
- `OTEL_EXPORTER_OTLP_*_HEADERS` (4 variants)
- `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_BEARER_TOKEN_BEDROCK`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `AZURE_CLIENT_SECRET`, `AZURE_CLIENT_CERTIFICATE_PATH`
- `ACTIONS_*` (GitHub Actions tokens)
- `ALL_INPUTS`, `OVERRIDE_GITHUB_TOKEN`, `DEFAULT_WORKFLOW_TOKEN`
- `SSH_SIGNING_KEY`

Also removes `INPUT_<key>` variants of each.

## Upstream Proxy Environment

The `registerUpstreamProxyEnvFn()` mechanism allows injecting proxy env vars
into all subprocesses. In Claude Code web, this merges fresh JWT proxy
credentials into the environment. The proxy env overrides `process.env` values.
