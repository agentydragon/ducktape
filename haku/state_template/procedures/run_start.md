# Run start — mandatory gates (execute before anything else)

These are **pre-conditions**, not suggestions. Run them in order before reading sources,
orienting on items, or writing anything date-sensitive. They cost under 60 seconds.

## 1. Operator-local date — derive it from the shell, right now

> Set `TZ` to **your operator's** timezone (the example uses `America/Los_Angeles` for an SF
> operator). All log filenames + human-facing date prose use the operator's local date, not UTC.

```bash
TODAY_SF=$(TZ=America/Los_Angeles date '+%Y-%m-%d')
NOW_SF=$(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M %Z')
echo "SF today=$TODAY_SF  now=$NOW_SF"
```

**Never use:** the system context `currentDate` tag, a bare `date` (both UTC), or arithmetic
from any of them. The trap: at SF evening, UTC has already rolled to "tomorrow" — this twice
corrupted log filenames and deadline math (called Jul 1 "TOMORROW" on Jun 28 PDT; created
`log/2026-06-29.md` when SF date was Jun 28).

Log filename = `log/$TODAY_SF.md`. All human-facing date prose = SF.

## 2. Bootstrap / haku-state ready

```bash
git -C ~/haku-state rev-parse --short HEAD && ls ~/haku-state/items | wc -l
```

Confirm HEAD exists and `items/` is populated. If not, wait or re-run bootstrap per the
web entrypoint instructions.

## 3. Quick bookmark sanity — Gmail epoch must be in the past

```bash
GMAIL_BM=<epoch from bookmarks.md>
NOW=$(date +%s)
[ "$GMAIL_BM" -gt "$NOW" ] && echo "CORRUPT FUTURE BOOKMARK — reset to now-1d" || echo "OK"
```

A future-dated Gmail bookmark silently returns 0 results every run (hit run 18 — blind for
~1 week). If corrupt, reset to `$(date +%s) - 86400` and note in the log.

## 4. Open today's log file

```bash
LOG=/root/haku-state/log/$TODAY_SF.md
# If it doesn't exist yet, create it with a header
[ -f "$LOG" ] || echo "# $TODAY_SF" > "$LOG"
echo "Log: $LOG"
```

Append run content to `$LOG` throughout. Never create a new file mid-run with a different date.

---

*After these four gates pass, proceed to the orient step in `haku/run.md`.*
