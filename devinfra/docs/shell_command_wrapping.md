# Shell Command Wrapping

Mechanisms for intercepting every command in bash/zsh so that `foo bar` actually
executes `your-wrapper foo bar`.

## zsh: `accept-line` widget override

Rewrites the command line buffer before the shell executes it. This is the most
practical shell-level approach.

```zsh
accept-line-wrapper() {
    BUFFER="your-wrapper ${BUFFER}"
    zle .accept-line
}
zle -N accept-line accept-line-wrapper
```

Every interactive command typed at the prompt gets `your-wrapper` prepended.

**Limitations**: Only works for interactive shells (ZLE widgets don't exist in
scripts). Naive string prepend breaks pipes, redirections, and compound commands.
A more robust version would need to parse `$BUFFER`.

## bash: `DEBUG` trap with `extdebug`

```bash
shopt -s extdebug
trap 'eval "your-wrapper $BASH_COMMAND"; return 1' DEBUG
```

With `extdebug`, returning non-zero from a `DEBUG` trap skips the original
command. The trap runs `your-wrapper` instead via `eval`.

**Limitations**: Fragile with quoting, subshells, and compound commands.
`$BASH_COMMAND` is a flat string, so arguments with spaces or special characters
need careful handling.

## Observational hooks (no execution replacement)

These see commands but don't replace them:

| Shell | Mechanism                               | Notes                                |
| ----- | --------------------------------------- | ------------------------------------ |
| zsh   | `preexec` function                      | Receives full command string as `$1` |
| bash  | `trap '...' DEBUG` (without `extdebug`) | `$BASH_COMMAND` has the command      |

Useful for logging, timing, or auditing — but the original command still runs.

## PATH shadowing

Place a directory of wrapper scripts first in `$PATH`:

```bash
# /usr/local/wrappers/git
#!/bin/sh
exec /usr/local/wrappers/.real-dispatch git "$@"
```

Works for specific commands but doesn't scale to wrapping _every_ command without
generating a wrapper for each binary on the system.

## `LD_PRELOAD` — libc-level `execve` interception

Intercept the `execve` syscall at the C library level:

```c
// wrap_exec.c
#define _GNU_SOURCE
#include <dlfcn.h>
#include <unistd.h>
#include <string.h>
#include <stdlib.h>

int execve(const char *pathname, char *const argv[], char *const envp[]) {
    typedef int (*real_execve_t)(const char *, char *const [], char *const []);
    real_execve_t real_execve = dlsym(RTLD_NEXT, "execve");

    // Build new argv: ["your-wrapper", pathname, argv[1], argv[2], ...]
    int argc = 0;
    while (argv[argc]) argc++;

    char **new_argv = malloc((argc + 2) * sizeof(char *));
    new_argv[0] = "your-wrapper";
    new_argv[1] = (char *)pathname;
    for (int i = 1; i <= argc; i++)
        new_argv[i + 1] = argv[i];

    return real_execve("/usr/local/bin/your-wrapper", new_argv, envp);
}
```

```bash
gcc -shared -fPIC -o wrap_exec.so wrap_exec.c -ldl
LD_PRELOAD=./wrap_exec.so bash
```

**Catches everything** that goes through libc `execve`, regardless of shell.
Doesn't work on statically linked binaries or setuid programs (loader ignores
`LD_PRELOAD`).

## seccomp / eBPF — kernel-level

For complete coverage including statically linked binaries:

- **seccomp-bpf**: Filter `execve` syscalls, but can only allow/deny/signal —
  can't rewrite arguments.
- **eBPF (`tracepoint/syscalls/sys_enter_execve`)**: Observe all `execve` calls
  system-wide. Read-only in tracing mode; `bpf_override_return` can block but not
  redirect.
- **ptrace**: Full control — can intercept `execve`, rewrite arguments, and
  redirect to a wrapper. This is what `strace` uses. High overhead per syscall.
- **Landlock / AppArmor / SELinux**: Policy-based execution control, not
  wrapping.

For true syscall-level command wrapping, **ptrace** is the only option that can
both intercept and rewrite `execve` arguments without kernel modules.

## Summary

| Approach                  | Scope                 | Can replace?      | Interactive only? |
| ------------------------- | --------------------- | ----------------- | ----------------- |
| zsh `accept-line`         | typed commands        | yes               | yes               |
| bash `DEBUG` + `extdebug` | typed commands        | yes (fragile)     | mostly            |
| `preexec` / `DEBUG` trap  | typed commands        | no (observe only) | yes               |
| PATH shadowing            | specific binaries     | yes               | no                |
| `LD_PRELOAD`              | all libc `execve`     | yes               | no                |
| ptrace                    | all `execve` syscalls | yes               | no                |
