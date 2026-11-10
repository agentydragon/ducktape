# Local `bb` and Bazel startup directives

The repo-pinned **bb 5.0.466** forwards startup directives from Bazel rc files.
The earlier observation that local `bb build` / `bb test` silently dropped them
was made with **5.0.339** and no longer describes the pinned CLI.

Checked 2026-10-30 against the pinned binary and upstream
[`d84e5dfd`](https://github.com/buildbuddy-io/buildbuddy/tree/d84e5dfd97439eeefbbf247681de47f3960adc11),
particularly `cli/parser/parser.go` (`ParseConfig`, `consumeAndParseRCFiles`,
`expandBazelRC`). The current parser expands rc files, including startup
options, then adds `--ignore_all_rc_files` so Bazel does not load them twice.
Do not infer that the startup options were lost from this flag alone.

## Verification

In an isolated scratch workspace with an empty `MODULE.bazel`, Bazel 9.2.0,
and this `.bazelrc`:

```text
startup --host_jvm_args=-XX:CliDocAuditInvalidOption
```

Running the pinned binary with `--nosystem_rc --nohome_rc`, an isolated
`--output_user_root`, and `version` produced this JVM error in `server/jvm.out`:

```text
Unrecognized VM option 'CliDocAuditInvalidOption'
```

That confirms the workspace startup directive reached the JVM. This probe is
intentionally invalid and belongs only in a disposable workspace.

## Session rc files still need to be selected

Forwarding startup directives does not automatically select the Claude
session's separate bazelrc. The session's `bazel` / `bazelisk` shims inject
`--bazelrc=<session-rc>`; the `bb` shim's selection policy is a separate question
tracked in <../../claude/TODO.md#session-setup-and-bazel>.

For local `bb`, explicitly pass `bb --bazelrc=PATH build //target` when that
file is needed. `bb remote` expands local `.bbrc` settings but leaves Bazel rc
expansion to the hosted runner; caller-local JVM trust settings are not
implicitly transferred. See <remote.md> for configuration sources
and the caller/runner boundary.
