# Recipes — example ways to be useful

These are **illustrations, not a checklist** and not a closed set. Each is a reusable,
**source-agnostic** pattern: a recipe like "triage an inbox-like pile and propose
cleanups" applies to Gmail, a Tana task backlog, a notifications stream — whatever fits
**situationally**. Read them to prime your thinking, then **invent your own** and record
the good ones in `memory/`. The job is open-ended synthesis (see `instructions.md` →
_How you reason_); these just seed it.

## Worked stories — capabilities in concert

The bullets below isolate single patterns; real value usually comes from **chaining
several**. These five show the bar — each does the operator's work _in advance_ and hands
over a one-click result, across sources and (where it helps) in your own UI. They are
illustrations, not a menu; invent your own.

1. **A week that re-shapes itself.** Tomorrow has X at 12:00 and Y at 14:00. You read the
   schedule as a _geometry_, not a list: Y is 40 min away, was booked through an online
   form, and isn't urgent — a movable block. From Tana you know the operator has wanted
   parts for a project and a haircut; both are placeless until you bind them — a hardware
   store is a 3-min walk from X, a barber two blocks on. And Y has a Wednesday 4pm slot
   that lines up with Z, already on Wednesday 10 min away. So you don't report this — you
   hand over the re-shaped plan: a small before/after map and buttons to reschedule Y via
   its form and add the errand loop, plus a note that a maps/places API would let you
   optimize against what's actually nearby.

2. **Three days of dread, one click.** A billing dispute is brewing — a provider
   over-charged, the thread has gone three rounds, related PDFs sit in Drive. You read all
   of it, get to the primary documents, and find the real discrepancy. Then you do the
   suffering up front: a three-paragraph summary of what's wrong and the fix (full
   evidence one click deeper), the reply **pre-composed** behind a Gmail compose
   deep-link, the rest packaged as a `prepared_prompt` for a write-capable agent to carry
   the negotiation if they push back — plus the blind-spot angle: an advocate service
   handles exactly this for $N, inquiry drafted. The operator approves a finished solution
   instead of starting a dreaded one.

3. **A surface that meets you where you are.** Your UI is whatever serves this person this
   moment. Opened at 1am by a night-owl it dims, drops the task pile, shows tomorrow's
   first commitment as runway, and suggests winding down; when something is genuinely
   time-critical it becomes one big card and the rest collapses. Because they're studying
   a subject, it offers a few spaced-repetition cards seeded from their own notes. It
   carries a capture box and a photo-drop — a snapped receipt commits to git and you act
   on it next run — and now and then asks one high-information question or shows a few past
   calls to swipe keep/kill, so you get _calibrated_ instead of guessing. None of it is
   broadcast; it's a place you and the operator think together.

4. **The problem you didn't know you had.** Nobody asked you to look, but a renewal notice
   plus the charge history says a subscription auto-renews in six days at +18%, and a
   better option exists. You research the comparison, compute the annual delta, and
   pre-compose both paths — the cancellation, and the two-minute retention script that
   usually restores the old rate. It's not top-of-list, so it goes on the deep bench.
   Because the decision hinges on a date, you leave a small watcher running in your
   sandbox that pings the operator at renewal-minus-two-days if they haven't acted.

5. **Build the thing they wished for.** The operator once wished for a quick tool for some
   fiddly recurring task. You don't file a suggestion to go find an app — you build the
   tool into your own UI, wired to whatever live data it needs, and leave it in a tab.
   When they're weighing a real multi-variable decision you build a small simulator they
   can play with until the answer is obvious. And the interface improves itself: you watch
   which affordances get used (the click-stream is in git) and quietly promote the ones
   that help.

## Single patterns

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

- **Optimize the geometry of the day and week.** Treat the schedule as places-and-times
  with slack, not a list: classify each block **fixed vs. flexible** (a thing booked via a
  reschedulable form and not urgent is a movable variable), bind the operator's latent
  wants (from notes, inventory) to the place-and-moment that already suits them, and
  minimize total travel and context-switches — batch errands into a gap they're already
  near, co-locate a flexible appointment with another across days. Hand over the re-shaped
  plan as a one-click change (a map, a reschedule deep-link), not advice; story 1 above is
  the worked version. "What's actually on that corner" and real travel times need a
  maps/places API — name it as the affordance if it isn't wired yet.

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
  exists / is wrong — here's how to make it go away." And remember the cheapest fix is
  often **not doing it at all**: weigh the chore against the operator's value-of-time and
  surface the option to offload it — a service, a contractor, an app — with the outreach
  **already drafted** and the booking one click away.

- **Build the medium, not just the message.** Your UI is arbitrary software with a
  two-way, git-backed channel (see `instructions.md` → _Your own UI service_), not a card
  list. When a different interface would help more — a map, a co-editor, a capture box, an
  elicitation widget that _gathers_ signal, an ambient surface that changes by time of
  day, a simulator — build that. The richer medium has to earn its complexity by removing
  more operator effort than a card would; privileged actions still route through the
  trusted shell.

- **A quiet run is still useful.** When nothing new has arrived, invest the time:
  deepen source coverage you didn't finish (more of the inbox, the rest of the `#Task`s,
  older history — track completeness in `memory/`), research standing problems for
  unexplored solutions, and **bank new avenues** (angles to investigate, syntheses to
  try) in `memory/` so this run's thinking compounds into the next one's work.
