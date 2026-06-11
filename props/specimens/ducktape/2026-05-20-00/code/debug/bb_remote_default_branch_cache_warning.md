# `bb remote` default branch cache warning

## Symptom

When running `bbr` from Claude Code, `bb remote` prints:

```
Warning: Failed to cache default branch "devel" in .git/config: failed to update
"buildbuddy.remote-bazel-default-branch" in .git/config
(chdir /home/agentydragon/code/ducktape: no such file or directory)
```

The warning is cosmetic — `bb remote` works fine, it just can't persist the
detected default branch name for next time.

## What `bb` is trying to do

`bb remote` detects the repo's default branch, then tries to cache it in
`.git/config` under the key `buildbuddy.remote-bazel-default-branch` so it
can skip detection on subsequent runs. The cache write fails.

## Interleaved output

The error message has `[git-shim] log: /tmp/claude/.../git-shim.log`
interleaved in the middle, meaning `bb` (Go binary) is spawning `git config`
as a subprocess. The `git` on PATH resolves to the Claude hooks git-shim,
whose stderr logging interleaves with `bb`'s error output.

## Likely cause: Claude Code sandbox `denyWithinAllow`

The Claude Code sandbox (bwrap-based, from `sandbox-runtime`) has a security
mitigation against bare-git-repo attacks. It unconditionally denies write
access to files that `git`'s `is_git_directory()` would use to treat CWD as a
bare repo:

```typescript
// sandbox-adapter.ts lines 257-280
const bareGitRepoFiles = ["HEAD", "objects", "refs", "hooks", "config"];
for (const dir of [originalCwd, cwd]) {
  for (const gitFile of bareGitRepoFiles) {
    const p = resolve(dir, gitFile);
    if (existsSync(p)) denyWrite.push(p);
    else bareGitRepoScrubPaths.push(p);
  }
}
```

This produces `denyWithinAllow` entries visible in the sandbox config:

```
"/home/agentydragon/code/ducktape/HEAD"
"/home/agentydragon/code/ducktape/objects"
"/home/agentydragon/code/ducktape/refs"
"/home/agentydragon/code/ducktape/hooks"
"/home/agentydragon/code/ducktape/config"
```

These deny `$CWD/config`, not `$CWD/.git/config`. So this specific mechanism
shouldn't block writes to `.git/config` directly.

## Open question

The exact failure path isn't fully traced. Possibilities:

1. **`bb` spawns `git -C <repo> config --set ...`** — the git-shim runs,
   contacts the hook daemon, then execs real git. Something in this chain
   fails and Go wraps the child error with the `Dir` field (`chdir <repo>`).

2. **The sandbox blocks `.git/config` writes via a different mechanism** — the
   sandbox also has app-level `isDangerousFilePathToAutoEdit` checks that
   block writes to `.git/` paths, but those apply to Claude's tool calls
   (Edit/Write), not to subprocess file I/O inside bwrap.

3. **`dangerouslyDisableSandbox: true` doesn't fully disable bwrap** for
   subprocesses spawned by shimmed binaries — the `bbr` shim execs `bb`
   which then spawns `git`. If only the outer shell command is unsandboxed
   but `bb`'s child processes still inherit some restriction, the `git config`
   write could fail.

4. **Transient issue** — `/proc/self/cwd` confusion or a race during shim
   exec.

## Impact

None. The warning is cosmetic. `bb remote` re-detects the default branch
every invocation (cheap) and proceeds normally.
