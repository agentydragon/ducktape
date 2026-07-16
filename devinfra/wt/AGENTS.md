@README.md

# Agent Guide for `wt`

## Pitfalls

- UNIX socket permissions can cause ECONNREFUSED in restricted sandboxes
- fd3 semantics in `wt.shell` are load-bearing -- don't break them
- `gitstatusd_listener` lifecycle must stay in sync with daemon startup
- COW behavior differs per platform; keep feature flags configurable via shared config models
