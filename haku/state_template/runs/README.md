# Runs — per-run propagation record

Each run writes a manifest here proving every source was processed and recording how each change
propagated to every surface it belongs on. The UI's **Runs** tab reads these. See the base
"Propagation discipline" obligation and `procedures/propagation/`.

One markdown file per run, ULID-keyed (like `items/`), date-foldered (like `log/`):
`runs/<YYYY-MM-DD>/<ulid>.md` — the propagation manifest as YAML **frontmatter** (the structured
spine, CI-validated against `RunManifest` and read by the UI), then free-form reasoning as the
markdown **body** (rendered in the Runs tab).

## Manifest schema (the `<ulid>.md` frontmatter)

```markdown
---
run_id: <ulid> # matches the filename
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
---

<free-form reasoning for the run — the judgment no checklist can capture>
```

`changes_seen: 0` and `action: no_change`/`n/a` are first-class — "considered, didn't apply" is
recorded, not absent. Keep the frontmatter lean; it's hand-written each run.
