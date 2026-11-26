# Agentplane frontend

## Service responses

Action, MCP linkage, and Connection adapters use the generated protobuf readers. Unknown numeric MCP
display states remain readable as `unknown (value)`; they do not become an allowed decision or
successful state. Required identities and timestamps, decision verdicts, mutation version checks,
and nonempty watch frames remain validated.

The MCP servers page shows each available group's tool schema warnings. An unvalidated tool remains
callable under its usual Action policy, but its arguments are not checked against that input schema.

## Action previews

The expanded Actions pane and receipt details show live Gmail subjects beside recorded thread IDs
for label changes, and live Tana names beside recorded node IDs for node moves. Lookup is optional.
The view can render the saved arguments immediately and keep showing them if the lookup fails.
The page and Web Push worker both use the operator-authenticated Action Service facade.

Pending GitHub file pushes show a per-file diff in the expanded Actions card and detail view
when the old text is available. The comparison identifies the branch commit it read.
GitHub resolves the branch again when it runs the push. Files always offer their complete
proposed contents.

Calendar approvals can resolve an event or series title, schedule, scope and Google Calendar link
through the operator facade. A missing preview leaves the request's calendar and event IDs
visible. Calendar receipts use recorded results, with the complete request and response available
from Raw.
[Notification settings](actions/push/README.md) control whether a browser can show these names
in a notification.

## Environment identity

The Sandbox Status tab shows the currently requested `spec.identity` name. Only a Sandbox whose
controller-reported stop stage is `Stopped` shows the identity selector and change button. Changing
or clearing it warns that the next identity can read files earlier agents left in the same
workspace. The app sends an exact namespace/name/UID target to EnvironmentService through its
authenticated facade and displays its admission refusal message. The "Retained identity record"
reads the current Sandbox-owned WorkloadCredential on demand and offers an explicit refresh. An
empty record is not historical proof: older credentials are revoked, and the UI says this is not
a complete history. The new-environment identity picker awaits the open creation-contract question.

## Session inventory

List membership, creation order, materialization and required Environment state come from the
Dispatcher Session watch, including Sessions not yet recorded in History. The browser uses the
operator-authenticated `/upstream/dispatcher/v1/sessions:watch` proxy and the generated
SessionDispatcher protobuf-es messages. Each browser stream has its own upstream watch. An empty
token supplies the snapshot and `CaughtUp`; there is no ListSessions bootstrap. The browser retains
its inventory and in-memory resume token across reconnects, advancing the token only after applying
a frame. `INVALID_ARGUMENT` clears the token and starts a fresh snapshot. A page reload starts empty;
no token crosses a browser login lifetime. Disconnection retains the last view with stale health.

Titles and archived state remain App metadata. Current activity comes from Dispatcher Session
observations: attached, dormant, active turn, pending commands, setup, Unknown and EnvironmentDown.
Running marks and active-turn controls follow those observations independently of History and
Environment inventory health. A disconnected Dispatcher watch immediately shows Unknown, and a
reconnected watch must reach `CaughtUp` before its observations become current. Unknown and
EnvironmentDown never imply idle. History supplies conversation content and the last completed
turn's outcome. Materialization never establishes that a harness is running. A Session with no
History summary appears with its materialization state. History-only records cannot add list
members. The App metadata watch refreshes cached metadata; the scoped History watch supplies
summary snapshots and changes for the visible and open Sessions. Dispatcher updates reselect the
window without refetching App metadata. A replacement summary watch retains the previous view until
its snapshot is ready.

The Thread page uses Dispatcher placement to identify the exact Environment before History exists.
Failed or unknown materialization and its reason are visible and command controls are disabled. Retained History remains visible when available. Missing identity
is reported as unknown; only an identified Environment absent from a fresh inventory is deleted.

The open Thread watches the Dispatcher's bounded Session workset. The conversation shows queued,
delivering, failed, and unknown commands using ordinary input bubbles and control cards. The browser
can load originals for visible receipts even before History is available. Loaded History owns
finished commands. A pending History command shows its unresolved delivery status on the same
row. The message gutter holds the progress dots, short state, and dismissal. The dots follow
submission, Dispatcher delivery, runner admission, and the observed effect. Red identifies a
confirmed error at its boundary. A recorded delivery-attempt error can be red while the outcome
remains unknown. Unknown outcomes without such an error leave the first unverified stage dashed.
The state opens its reason without resizing the message row. Admissions and cancellations outside
the conversation are available in Delivery history.
The browser reports stale delivery updates, clipped active work or issues, and expired receipt
coverage. When a reason is abbreviated, the browser reads the full retained reason by command ID.

An operator can acknowledge a failed or unknown Dispatcher delivery issue. The App stores the
preference for the verified OIDC operator, Thread, command ID, and exact receipt revision. Another
browser signed in as that operator sees it when it opens or returns focus to the Thread. A newer
receipt revision needs separate attention. When the workset clips issues, Older delivery issues
opens the paginated receipt history, where retained failures can still be acknowledged.
The selected Thread shares acknowledgement state between the conversation and Delivery history.
Opening the archive loads one page at a time, with input excerpts and requested settings from
retained receipts. Operators can open the full stored command. Dismissing a receipt updates both
views.
The browser checks saved local terminal rows against bounded Dispatcher batch reads before
hiding an equivalent acknowledged notice, including after it leaves the recent workset. Without
an exact server revision the notice remains visible. History and saved command data are unchanged.
Delivery history remains available independently of the History projection and includes previous
acknowledgements. When an exact revision has expired or was superseded, the interface reports that
loss of evidence.

Archiving refuses active turns, pending commands, running setup, harness startup and Unknown.
The App mutation handler reads Dispatcher `GetSession` on each request before writing the archive
flag, so browser state cannot bypass the refusal. Confirmed suspended or deleted Environments can
be archived. This check does not prevent new work admitted after the read by another service.

## Thread creation

When Create thread is disabled, hovering it or focusing its wrapper shows the current reason:
Sandbox availability, an unconfirmed Session creation, Session materialization in progress, or model availability.
When creating a Thread on an existing Sandbox, the form shows that Sandbox's Identity and current
Identity bindings read-only. It distinguishes an unassigned Identity from a missing Identity resource,
and keeps policy-watch loading and failures visible.

## Sandbox deletion and identity cleanup

The Sandbox list and page offer an independent identity-cleanup choice in the delete dialog when the
selected Sandbox names an Identity. The choice starts off and is unavailable for a Flux-owned Identity,
an incomplete or unhealthy Sandbox/Policy watch, a failed Session inventory, or any other Sandbox,
Session, or unowned PolicyBinding still naming that Identity. Deleting the Sandbox alone remains
available. A selected cleanup waits for the Sandbox to disappear from the live inventory before
deleting the Identity, and shows a refusal or error with a retry action if cleanup cannot finish.

The browser's paginated Session scan and live watches are advisory, not an atomic reference snapshot.
The Policy Controller also rejects its known PolicyBinding and WorkloadCredential references, but
currently does not enforce Sandbox or Session references at DeleteIdentity admission. A concurrent
new Sandbox or Session reference can race cleanup; launch and delivery must fail closed if the named
Identity no longer exists. Default-on cleanup requires an agreed app-minted origin marker, which is
not yet present on Identities.

## Thread names

An operator can set, rename, or clear a Thread name from the thread view. An unnamed Thread displays
its ID as a placeholder; clearing the name restores that placeholder.

## Thread command outcomes

The thread shows command failures, refusals, unconfirmed Dispatcher receipts and unknown delivery
outcomes below the command, including the reason when available. For an attempted command without
a receipt, reload, reconnect and Retry first read its Dispatch by ID. The full command and Session must
match the saved record. A missing live Dispatch allows the browser to retry a Session-addressed command
with its original ID and payload. An incomplete or contradictory receipt, or a lookup error, leaves
the command unconfirmed. Older commands with uncertain or Sandbox-addressed destinations offer
Check status and retain their original input.

## Composer activity

The composer shows the controller's next idle-suspension deadline from the current live Sandbox
snapshot, with a client-side countdown and the full deadline on hover. A stale snapshot reports the
timing as unavailable; a Sandbox that has not appeared yet shows no autosuspend state. Model activity
and turn completion remain separate signals; when Dispatcher confirms idle and their relative ages
match within a minute, the composer combines them while keeping the exact idle timestamp on hover.

## Command delivery updates

When the Dispatch watch disconnects, the thread warns that queued and delivering states may
be stale. Stream errors appear immediately. Each connection has one minute to reach its
first checkpoint before the browser reports a timeout and retries. A clean closure gets a
three-second grace period for expected watch lease renewals. The warning clears when the
browser applies and saves a `CaughtUp` checkpoint or has no commands left to watch. Previously
saved receipts remain visible, and History continues to supply command outcomes. A warning
about expired deletion coverage remains after recovery.

## Runtime diagnostics

The Status tab reads the generated EnvironmentService diagnostics facade through the authenticated
app. It requires matching namespace/name/UID, a workload incarnation, an observation timestamp and
a Runtime boot ID before displaying a snapshot. Missing or mismatched evidence is unavailable.

The Status tab shows the Runtime's desired lifecycle mode and generation alongside the aggregate
controller's published stage. When progress belongs to the desired generation, it also shows the
controller's first unmet barrier and its resource/process identity, available admission count,
journal seal identity, acknowledgement cursor, and surfaced lifecycle error. When progress belongs
to an older generation, it is labeled as last progress and its blocker and error are withheld.

## Policy binding status

A reader's refusal applies only to the binding generation it observed. A condition for another
generation keeps the binding "Not yet judged" until readers catch up. A current refusal or a
missing selected PolicySet still shows "Refused".

## Tool calls

A lone tool call renders its compact tool summary directly. Only runs of multiple steps have an
additional group summary. Expanding a tool call reserves room for its arguments and output before
the next history row.

## Thread history scrolling

A thread's conversation can grow long, include messages of different heights, and change while
someone is reading it. The following are promises to the reader; they do not prescribe how the
browser loads, stores, or draws the conversation. These guarantees cover the supported Chromium
browser on desktop and phone.

### Guarantees

- **Opening a thread** — including switching from another thread — scrolls to the thread's tail;
  the most recent message is what the reader sees first.
- **Recent history is immediately readable** — opening a thread shows enough preceding conversation
  to scroll naturally without a page-by-page wait. A short thread shows its full history.
- **Older history arrives before it is needed** — scrolling toward the start of the visible history
  reveals earlier messages without stopping at an empty edge.
- **Returning to a retained thread** shows its saved messages immediately. A status at the end of
  the history says it is checking for updates until the window catches up.
- **Reloading or opening a second tab in the same login** resumes a saved window after History
  confirms its scope. The browser stores the rows, page boundary and Electric resume position
  together in IndexedDB. Complete payload revisions live separately and load as their rows are
  displayed. Missing bodies are fetched individually or in batches.
- **Local storage is disposable.** Saved history is plain IndexedDB data, scoped to the operator's
  account, thread and projection epoch. Opening a thread requires a successful scope read. Explicit sign-out removes
  the profile’s thread-history bucket, including copies held on disk by other tabs. Browsers without
  named storage buckets keep history in memory. Storage pressure sheds cold bodies before windows.
  Unavailable storage falls back to live reads; drafts and pending input are outside this cache.
- **An appended message, reader at the bottom**: the view follows it into view. "At the bottom"
  tolerates a small amount of slack — a reader a few pixels off the exact bottom (scroll momentum,
  subpixel rounding) still counts as following.
- **An appended message, reader scrolled up**: nothing moves. The reader keeps reading exactly
  where they were.
- **A message actively streaming, reader following, it is the last message**: the view tracks its
  growth so the growing edge stays visible. When the message is taller than the viewport, its
  growing tail takes priority over keeping its start visible.
- **A message actively streaming or otherwise resizing anywhere else** — off-screen above or below
  the viewport, or while the reader is not following — must not move the reader's scroll position,
  even by a pixel.
- **Opening or closing a row** — a run, a tool call, an output past its height cap — keeps the row
  the reader clicked in place, growing or shrinking below it, while messages arrive or the reader
  follows the bottom. The click disengages following until the reader returns to the bottom,
  unless the whole thread fits the view and has nothing to scroll away from.
- **A deliberate scroll** (wheel, drag, keyboard, touch) away from the bottom disengages following
  immediately, without lag or fighting the input.
- **Returning to the bottom** re-engages following, with the same tolerance as above.
- **A running thread whose end is off screen** — the reader scrolled up, or opened a row that grew past
  the view — shows a "Jump to latest" control; one click returns to the end and resumes following.
- **Scrolling into older messages** keeps the row the reader was viewing in the same place as
  earlier content appears — no blank flash or temporary jump, even in a short thread.
- **A window/viewport resize** (browser resize, mobile address-bar collapse, orientation change, an
  on-screen keyboard opening): following stays pinned to the bottom through the resize; not
  following keeps the reader's current row in place and shifts only off-screen content.
- **Text selection, copy, and find-in-page** work across the entire thread, not just the
  portion currently on screen.
- None of the above ever produces a visible flash of blank space or a scrollbar that visibly jumps
  and then corrects.

## Thread view recorder

Thread view recorder can record scrolling across Thread switches and download the recording locally.
The recording includes Thread/view identities, bounded row geometry and virtualizer state, and
before/after geometry for component scroll corrections. It excludes message bodies and tool
payloads. Layout sampling runs only while recording; the export reports truncation when its
snapshot limit is reached.
