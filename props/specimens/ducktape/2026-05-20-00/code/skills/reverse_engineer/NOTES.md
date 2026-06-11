# Notes

## `go_binary` vs `plain_binary` for v1_plain

We tried switching from the custom `plain_binary` Starlark rule to the standard
`go_binary` rule from rules_go. It doesn't work for this use case.

**Root cause**: rules_go builds binaries using `go tool compile`/`go tool link`
directly, bypassing the normal `go build` module infrastructure. The resulting
binary embeds only the Go toolchain version (`go1.26.2`) in its buildinfo — no
`path` or `mod` fields. So `go version -m v1_plain` outputs just:

```
v1_plain: go1.26.2
```

**Why this matters**: The binary diff recipe uses `go version -m` to contrast a
plain binary (full module info) against a garbled one (`unknown`). A rules_go
binary looks like neither — it defeats the demonstration.

**`plain_binary` works because** it does a real `go build` with a proper
`go.mod` (`module garble_target`), which causes Go to embed full buildinfo
including the module path. That's also why `go version -m v1_plain | grep
"garble_target"` works in the recipe.

**Keep `plain_binary`** in `defs.bzl`. Don't switch to `go_binary`.
