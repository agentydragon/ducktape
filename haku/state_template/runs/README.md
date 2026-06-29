# Runs — per-run propagation record

Each run writes a manifest here proving every source was processed and recording how each change
propagated to every surface it belongs on. The UI's **Runs** tab reads these. See the base
"Propagation discipline" obligation and `procedures/propagation/`.

Two files per run, ULID-keyed (like `items/`), date-foldered (like `log/`):

- `runs/<YYYY-MM-DD>/<ulid>.yaml` — the structured spine (CI- and UI-readable)
- `runs/<YYYY-MM-DD>/<ulid>.md` — free-form reasoning (rendered as markdown in the Runs tab)

## Manifest schema (`<ulid>.yaml`)

```yaml
run_id: <ulid> # matches the filename + the .md sibling
date: "YYYY-MM-DD" # operator-local (the log/ date)
started: <iso8601>
finished: <iso8601>
sources: # one row per source — declare EVERY source, scanned or skipped
  - { source: gmail, bookmark_before: ..., bookmark_after: ..., changes_seen: <int> }
  - { source: tana, skipped: "<why it was not scanned>" }
checklists: # which procedures/propagation/*.md were walked this run
  - { checklist: kitchen, ref: procedures/propagation/kitchen.md, walked: true }
propagation: # change-set → where it landed (the judgment record)
  - change: "<what changed>"
    source: <source>
    surfaces:
      - { surface: "<surface>", action: updated|no_change|n/a, note: "<why>" }
```

`changes_seen: 0` and `action: no_change`/`n/a` are first-class — "considered, didn't apply" is
recorded, not absent. Keep it lean; it's hand-written each run. The free-form judgment that no
checklist can capture goes in the `.md` sibling.
