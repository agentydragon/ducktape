# inbox_cleanup (example)

The companion to [`gmail_triage`](gmail_triage.md): that one pulls the **signal**
(threads needing a reply, deadlines, anomalies) out of mail; this one proposes
killing the **noise** — clusters of low-value mail the operator would happily
bulk-archive, label, filter, or unsubscribe from, so the inbox stays scannable.
Same read-only Google token (`../instructions.md` → _Hard rules_). You only ever
**propose** the cleanup; an executor session with Gmail write access creates the
filters / labels / unsubscribes (Haku's token can't).

## Gotcha: the inbox is not "everything that arrives"

Gmail's `INBOX` is a label, not the firehose — a lot of mail already skips it
(existing filters, the `CATEGORY_PROMOTIONS`/`SOCIAL`/`UPDATES` tabs, prior
auto-archiving). So separate two questions:

- **What actually lands in the inbox now** — `in:inbox newer_than:120d`. This is
  what's costing the operator attention; cleaning it is the high-value win.
- **Total volume of a sender/pattern** — the same query without `in:inbox`.
  Something can be huge in All Mail yet already skip the inbox (a CI bot that's
  already filtered); proposing a "skip inbox" filter for it is a no-op. Check
  before you suggest.

`resultSizeEstimate` is unreliable — to size a cluster, page `messages.list` and
count the ids you get back, don't trust the estimate.

## How to find clusters

Pull `in:inbox` mail since your bookmark, fetch `format=metadata` (headers
`From`, `Subject` + `labelIds`), and **tally by sender domain and by category**.
The long tail of a single domain, or a category that's almost all one kind of
blast, is a cluster. For each candidate, write the **exact `q=` search** that
isolates it and the real count — those searches are the deliverable (they become
the filter the executor installs and a one-shot bulk-archive query). Recurring
cluster shapes (adapt, don't treat as fixed):

- **Dead-lead property mail** — after a move, every apartment the operator
  toured-but-didn't-rent keeps marketing to them (`from:<leasing-domain>`,
  subjects like "Apartment Inquiry at …"). Safe to archive + label once the lease
  is signed. **Protect the current landlord and active move vendors** (lease,
  resident portal, movers, renters-insurance) — never archive those.
- **Event invites that already happened** — meetup / Luma / Mailchimp group
  blasts, support-group "tonight at 6pm" notices, announce lists. Past-dated ones
  are pure noise; a filter can auto-archive the sender and the operator keeps only
  what they deliberately add to the calendar.
- **Marketing / surveys / newsletters to unsubscribe** — recurring senders the
  operator never opens (product-update blasts, store promos, analytics digests,
  magazine newsletters). Here the right proposal is often **unsubscribe**, not
  just filter — collect the `List-Unsubscribe` targets so the executor can action
  them.
- **Expert-network & recruiter solicitations** — "paid consultation call" / cold
  recruiter blasts. Honor the operator's recruiter calibration (in your
  `memory/`) before proposing anything but a filter.
- **Automated receipts & low-value notifications** — payment-processor receipts,
  ride/delivery confirmations, order-shipped mails, app activity pings. Usually
  worth a "skip inbox + label `Receipts`" filter rather than deletion (they're
  occasionally needed for returns/taxes).
- **CI / build-bot floods** — `Run failed` / `Failed pipeline` notifications can
  dominate All Mail. Check `in:inbox` first: if they already skip the inbox
  there's nothing to file; if they don't, propose a filter (skip inbox, label,
  optionally auto-mark-read). A persistently-red pipeline is _also_ a
  `gmail_triage` finding — fix the build, don't just mute it.

## Filing

Prefer **one `prepared_prompt` per cleanup pass**, not one per cluster — a single
item whose prompt carries a table of `(cluster, exact q= search, count,
proposed action: archive / label / filter+skip-inbox / unsubscribe / keep)` so an
executor can review and apply them in one sitting. Keep an explicit **KEEP list**
(current landlord, insurer/EOB senders, real human threads, vendors, future
appointment reminders) in the prompt so nothing important is swept up. Bias to
label-and-archive over delete; never propose deleting anything that could matter
for money, health, or legal. Record which clusters you've already proposed in
your `memory/` so you don't re-file the same cleanup every run.
