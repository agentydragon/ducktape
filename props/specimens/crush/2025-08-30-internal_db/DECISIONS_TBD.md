# Decisions — To Be Decided (TBD)

This file tracks small refactors or style changes we’ve identified but haven’t decided to apply yet. Each item includes
context and a proposed rewrite for later review.

---

## LSP watcher: co-locate config reads and fold cfg nil-check

- File: `internal/lsp/watcher/watcher.go` (constructor: `NewWorkspaceWatcher`)
- Status: TBD — deduplication/cleanup; no behavior change
- Rationale: Avoid duplicated `cfg != nil`/`config.Get()` and co-locate `WatchMode` / `RecursiveMaxWatchedDirs` reads
  into one block. Line count is roughly unchanged but reduces repetition and keeps related config in one place.

Before

```go
cfg := config.Get()
mode := "recursive"
if cfg != nil {
    if lspCfg, ok := cfg.LSP[name]; ok {
        if lspCfg.WatchMode != "" { mode = lspCfg.WatchMode }
    }
}

maxDirs := int64(5000)
if cfg != nil {
    if lspCfg, ok := cfg.LSP[name]; ok {
        if lspCfg.RecursiveMaxWatchedDirs > 0 { maxDirs = int64(lspCfg.RecursiveMaxWatchedDirs) }
    }
}
if mode == "recursive" && maxDirs <= 0 {
    maxDirs = 5000
}
```

After (proposal)

```go
mode := "recursive"
maxDirs := int64(5000)

if cfg := config.Get(); cfg != nil {
    if lspCfg, ok := cfg.LSP[name]; ok {
        if wm := lspCfg.WatchMode; wm != "" {
            mode = wm
        }
        if r := lspCfg.RecursiveMaxWatchedDirs; r > 0 {
            maxDirs = int64(r)
        }
    }
}

if mode == "recursive" && maxDirs <= 0 {
    maxDirs = 5000
}
```

Notes

- This doesn’t reduce nesting dramatically, but it removes duplicated `cfg != nil`/Get and groups related config usage,
  which can help future edits.
- The magic constants (`5000`, `"recursive"`) are covered by the separate “magic constants should be named” finding in
  README.
