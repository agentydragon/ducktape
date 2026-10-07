## Duplication

### Resolution of relative/empty path (`internal/llm/tools/{view,write,edit}.go`)

Each tool repeats the same join logic:

```go
if !filepath.IsAbs(filePath) {
    filePath = filepath.Join(workingDir, filePath)
}
```

LS also separately defaults empty path to workingDir. Put both behaviors into a single resolver to avoid 3 copies.

## `internal/llm/tools/write.go` reads same file content twice

Two reads of the same file occur in close succession (oldContent at ~148–151 and again at ~161–167). Instead read once
and reuse for equality check, diff, and history recording.

---

## Round‑trip HTTP logging body buffering (neutral note)

- internal/logging/httpclient.go: 20–47, 49–63
- Summary: Round-trip logging loads request/response bodies into memory (io.ReadAll) to produce logs.
- Decision: This is acceptable and required by design because the same bodies are surfaced to the UI; therefore “loads
  body into RAM” alone is not a defect here.
- Action: Keep as a neutral note (neither TP nor FP). Evaluate other logging concerns (timeouts, redaction, size caps)
  separately.

## Red herring: ListLatestSessionFiles session scoping

- internal/db/files.sql.go: 206–215; internal/db/sql/files.sql: 47–55
- The grouping bug (latest per path globally, then WHERE session_id) is technically incorrect, but it’s a red herring
  today because the entire API is dead (no production callers; only a test fake references it).
- Keep this note as context; the true TP is the dead API (see issues/038-dead-api-list-latest-session-files.libsonnet).
  If the API is revived, fix semantics to per-(session_id,path) before usage.

## Multi‑session file versioning semantics (neutral note)

- internal/history/file.go: 58–75; internal/db/sql/files.sql: 27–36 (ListFilesByPath), 14–24 (GetFileByPathAndSession)
- Observation: CreateVersion derives the next version from ListFilesByPath(path) (global by path), while the uniqueness
  key is (path, session_id, version). This can make a session’s local version numbers jump or interleave with another
  session’s edits to the same path.
- UX smell: When multiple sessions write to the same path, any cross‑session view (or tools that reason about “latest
  per path” globally) can appear as a union/interleave of both sessions. Current TUI uses ListBySession for diffs (so
  per‑session views are OK), but overall semantics for cross‑session same‑path edits are unclear.
- Action: Decide intended semantics:
  - Are versions intended to be per‑session or global per path?
  - Should any cross‑session “latest” view exist? If yes, define rules; if no, remove dead snapshot API and keep
    per‑session only.
  - If per‑session is desired, consider deriving next version from per‑(session_id, path) max within the transaction to
    avoid surprising jumps.
