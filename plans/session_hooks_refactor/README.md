# Session hook refactor — prototype

Minimal task engine + a three-task slice (`secrets.buildbuddy_api_key → bazelrc.buildbuddy → bazelrc.session`) to eyeball the ergonomic shape before porting the rest of the session start hook.

## Design

Each task is a shell command with declared dependencies. The engine:

- Runs tasks in topological order, parallel where deps allow
- Gives each task an `$ENV_OUT` file it can append exports to
- Sources transitive deps' `$ENV_OUT` files into each task's environment
- Captures stdout/stderr per task into in-memory buffers
- Drains buffers + a session mailbox on demand into a single output blob
- Composes the final session env file as `cat $session_dir/envs/*.env`

No capture modes, no typed produced values, no skip-if expressions. Failures are logged but never abort the session. Downstream tasks self-skip on missing env vars (`[[ -z $FOO ]] && exit 0`).

## Layout

```
plans/session_hooks_refactor/
├── engine.py             # Task + Engine + drain
├── prototype.py          # Driver: load profile.yaml, run engine, print drain + env
├── profile.yaml          # Task declarations
└── tasks/
    ├── secret_sops.sh       # Generic sops extractor
    ├── bazelrc_buildbuddy.sh
    └── bazelrc_session.sh
```

## Run

```
python3 plans/session_hooks_refactor/prototype.py
```

Writes to `/tmp/claude-prototype/<session_id>/`.
