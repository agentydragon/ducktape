# Docker exec process lifetime

Each MCP session shares one persistent Linux container. Images must provide `/bin/sh`, `sleep infinity` for session startup, and `/usr/bin/timeout` with GNU-compatible `--signal=KILL` and fractional-second durations. Callers supplying another image are responsible for these prerequisites. No helper binary is copied into the image.

Each call runs `/usr/bin/timeout --signal=KILL <duration>s <original argv...>` through Docker exec, preserving literal arguments, environment, user, working directory, and separate stdout/stderr. Standard input is closed. The utility enforces the deadline inside the container, including ordinary children that remain in its process group; unrelated concurrent execs are unaffected. A missing requested executable returns status 127 with stderr.

Status 137 at or after the deadline is reported as `TimedOut`, retaining partial output. An earlier status 137 and other command exit statuses are preserved. A command's own status 137 near the deadline is indistinguishable from the timeout's SIGKILL. A separate five-second allowance bounds Docker output/exit reporting; expiry raises an error rather than claiming confirmed termination.

Caller cancellation and output-collection errors close the stream and propagate. Immediate remote termination is not guaranteed; the command remains subject to its original in-container deadline. The utility stops monitoring when its command exits: children outliving that command, deliberately detached groups, and inherited output held open by such children are outside the guarantee. Redirected background work can outlive a successful call. Session teardown kills and removes the whole container.
