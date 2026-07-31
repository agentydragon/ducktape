## Consider a `login` flag on exec calls

`hostexecd` now sets `HOME`/`USER`/`LOGNAME`/`SHELL` from the target account's passwd
entry and defaults the cwd to its home, which is what `su` and `runuser` do. It does
**not** source the user's profile, so anything that lives in `~/.bashrc`,
`~/.profile` or a direnv-managed environment is still absent — a caller wanting the
repo's `direnv` environment has to say so explicitly:

```bash
cd ~/code/ducktape && eval "$(direnv export bash)" && ...
```

Worth considering an opt-in `login: bool` on the exec request that runs `bash -lc`
instead of `bash -c`. Deliberately not done up front: a login shell sources arbitrary
user profile into an approval-gated remote execution path, which is a much larger and
less predictable surface than four variables derived from `getpwnam`. And it would
still not give direnv, which hooks interactive shells rather than login ones — so the
explicit `direnv export` above stays necessary either way.

Decide it on evidence: if callers keep hand-rolling the same profile preamble, the
flag earns its place.
