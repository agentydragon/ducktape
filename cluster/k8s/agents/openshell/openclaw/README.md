# OpenClaw OpenShell integration

## Mirror-mode disposable work

OpenClaw uses OpenShell workspace mode `mirror` with sandbox scope `agent`.
`/sandbox` is synchronized with the gateway-local agent workspace before and
after each foreground `exec`, and every agent session shares the same sandbox.

Use a uniquely named directory under `/tmp` for disposable clones, builds, and
other long-running commands that do not need to become part of the agent
workspace. `/tmp` is outside the mirrored `/sandbox` tree, so a later
local-to-remote synchronization does not overwrite it.

The OpenShell plugin currently synchronizes `/sandbox` back when the initial
`exec` call returns. If a command yields and continues through the `process`
tool, changes made after that yield are not synchronized back; the next
`exec` can replace them from the stale gateway-local workspace. Keeping that
work under `/tmp` avoids this mirror-mode retention bug.

`/tmp` is disposable, lives only as long as the sandbox pod, and is shared by
concurrent sessions for this agent. Use unique paths to avoid collisions, and
commit and push any result that must survive sandbox recreation.

OpenClaw's native `read`, `write`, `edit`, and `apply_patch` tools resolve paths
on the gateway side and only bridge the managed `/sandbox` and `/agent` roots.
They cannot access the sandbox's `/tmp`; an absolute `/tmp` path is interpreted
as a gateway-host path and rejected by the workspace boundary check. Use shell
commands such as `git`, `sed`, and `cat` through `exec` for all interaction with
files under the sandbox's `/tmp`.

OpenClaw also only accepts `exec` working directories under its managed
workspace roots. Keep the tool `workdir` under `/sandbox` and change directory
within the shell command, for example:

```bash
git clone https://github.com/agentydragon/ducktape.git /tmp/ducktape-$TASK_ID
git -C /tmp/ducktape-$TASK_ID status
```
