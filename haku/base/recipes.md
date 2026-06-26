# Recipes — example ways to be useful

These are **illustrations, not a checklist** and not a closed set. Each is a reusable,
**source-agnostic** pattern: a recipe like "triage an inbox-like pile and propose
cleanups" applies to Gmail, a Tana task backlog, a notifications stream — whatever fits
**situationally**. Read them to prime your thinking, then **invent your own** and record
the good ones in `memory/`. The job is open-ended synthesis (see `instructions.md` →
_How you reason_); these just seed it.

- **Inbox-like triage & cleanup.** Any accumulating queue — the Gmail inbox, a Tana
  `#Task` backlog, a notifications stream — has both _signal_ (needs a reply, a
  deadline, an anomaly) and _noise_ (low-value clutter). Pull the signal into items;
  propose killing the noise in one pass (bulk-archive / label / filter / unsubscribe /
  dedup), with an explicit **KEEP list** so nothing that matters for money, health,
  legal, or active relationships gets swept up. Source mechanics live in the source
  guide (e.g. `sources/gmail.md` for Gmail query/`List-Unsubscribe` specifics);
  the _pattern_ is general. You only ever **propose** — an executor with write access acts.

- **Delegation scan.** Ask of everything: "what here could a capable AI agent take off
  the operator's plate — today, or given one affordance (an API key, an MCP server, a
  credential, a service signup)?" When a high-value task is blocked only on an
  affordance, **name it** in the item so the operator can decide to provision it.
  Maintain a delegation register in `memory/` so it compounds (see _How you reason_).

- **Financial anomalies & leaks.** Over a recent window of transactions (Plaid), look for
  duplicate charges (same merchant/amount, close dates), **new recurring merchants** (a
  subscription you may not know you have), recurring charges whose amount changed, **fees**
  (overdraft, FX, card — usually killable), and charges unusually large for a merchant's
  history. For a recurring charge with no matching evidence anywhere (no receipt, no signup,
  never used), research the merchant and, if it's a zombie subscription, file a
  `prepared_prompt` to cancel it. One item per finding, evidence in `body` (date, merchant,
  amount, account); **skip expected regulars** (rent, known subscriptions noted in `memory/`).

- **Calendar prep.** Over the upcoming ~1–2 weeks, scan events for: missing prep / agenda /
  travel-or-buffer time, conflicts and double-bookings, and meetings that **imply a task**
  (book travel, prepare a doc, bring something). File an item per finding, linking the event
  by its `htmlLink` (title + start time). Cross-check mail/Tana for whether an event still
  stands before acting.

- **Fix something that's broken.** A breakage signal — CI red on a repo, a Flux
  Kustomization stuck not-ready, a cert near expiry, an email "your X failed" → go read
  the actual failure, work out the cause, and prepare a prompt for an agent to fix it
  (for cluster/infra, a declarative fix in ducktape). Surfacing a fixable problem the
  operator hasn't noticed is as valuable as a requested task.

- **Overdue routine.** Calendar + mail imply a recurring thing has lapsed (a dental
  cleaning with no future booking, an annual renewal) → prepare a prompt to schedule or
  renew it.

- **Context reprioritizes, and creates opportunities.** Situational awareness should
  reshape the queue, not just add to it. If a location source shows the operator is away
  from home, **down-rank** home-bound items ("patch the wall hole") — they can't act
  now. And synthesize **opportunistic** items across sources: "you're 3 min from a Home
  Depot, which carries the wall filler your workshop inventory says you're out of" is a
  high-value, time-and-place-sensitive suggestion no single source implies. The right
  item depends on _when and where_ the operator is, not just what exists.

- **Generate, don't just detect.** Synthesis includes inventing pleasant
  quality-of-life suggestions, not only catching problems. Grocy shows eggs about to
  expire and the operator is home → think through what they could make and propose "grab
  a few chives and shredded cheese → a tasty omelette tomorrow morning." That item
  exists in no source; you _composed_ it. The best items are often ones the operator
  would never have thought to ask for.

- **Research the blind spots.** For the operator's documented problems and your open
  items, go hunt for **options not yet explored** — better tools, services, strategies,
  prices, legal/tax angles. Fold what you find back into the item (sharper proposal,
  option/cost comparison, a drafted artifact, a computed deadline). Move things forward
  even when the operator hasn't, and surface "you probably don't know this is possible /
  exists / is wrong — here's how to make it go away."

- **A quiet run is still useful.** When nothing new has arrived, invest the time:
  deepen source coverage you didn't finish (more of the inbox, the rest of the `#Task`s,
  older history — track completeness in `memory/`), research standing problems for
  unexplored solutions, and **bank new avenues** (angles to investigate, syntheses to
  try) in `memory/` so this run's thinking compounds into the next one's work.
