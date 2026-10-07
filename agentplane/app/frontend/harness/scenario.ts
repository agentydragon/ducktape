/**
 * What `harness.tsx` reads of a row in either fixture table: the route it mounts and the fixture
 * variations that route alone does not express. Python captures from `scenarios.json` in the
 * generic sweep and drives `interaction_scenarios.json` with named Playwright tests. BUILD names
 * no individual scenario.
 */

import table from "./scenarios.json";
import interactionTable from "./interaction_scenarios.json";

export type DisclosureVisualStage =
  | "collapsed"
  | "short-expanded"
  | "long-top"
  | "long-scrolled"
  | "after-disclosure"
  | "nested-parent-only-scrolled"
  | "nested-child-scrolled"
  | "nested-after-child"
  | "nested-after-outer"
  | "nested-wrapped-headings"
  | "nested-before-output"
  | "nested-expanded-output"
  | "nested-after-output"
  | "nested-output-collapsed";

export interface Scenario {
  /** Sanitized staging-derived run, preserving the sequence and relative sizes of its items. */
  realisticRollout?: "completed" | "reported";
  /** Mount the isolated shared-disclosure phone scene instead of the full app. */
  disclosureVisual?: DisclosureVisualStage;
  /** Offer Codex only while retaining existing Claude threads. */
  claudePaused?: boolean;
  /**
   * The app's hash route. The harness sets it before mounting, so App's router picks the view.
   * `/` is the threads view, so the launch form is `/sandboxes`. A route can carry view state in its
   * query: `?tab=egress&rules=<binding>` opens that binding's rules, so the shot carries its
   * credential detail (description, where the proxy puts it, which secret it comes from) beside the
   * other binding's folded row. `/mcp-servers` is where the MCP-linkage OAuth callback lands:
   * Settings, open on its MCP servers tab.
   */
  route: string;
  /**
   * Serve a watch that has stopped cycling: the staleness banner is the page saying so. On a thread
   * whose sandbox the stale inventory lacks, it cannot say the sandbox was deleted.
   */
  wedgedWatch?: boolean;
  /** Show failed Kubernetes grant provisioning in the Sandbox status summary. */
  grantError?: boolean;
  /** Show live pending Actions in the shell and thread composer. */
  pendingActions?: boolean;
  /** Make the SSH command taller than the inline review and scroll to its decisions. */
  longPendingAction?: boolean;
  /** Render a bounded first history page with a Load more control. */
  historyPaged?: boolean;
  threadlessSandbox?: boolean;
  /** `disconnected` drops the sidebar's own stream after its first snapshot; `database-disconnected`
   * keeps it up but reports the server's database feed down. */
  sidebarSource?: "disconnected" | "database-disconnected";
  /** Drop the sandbox inventory stream after its first snapshot, leaving the sidebar's own stream
   * up. */
  inventoryDropped?: boolean;
  /** How long, in ms, the streams this scenario drops have been down when it renders. Without it
   * they have only just dropped, which shows nothing. */
  outageAge?: number;
  /** Exercise the production scope and Electric shape synchronization boundary. `unavailable`
   * is a persistent initial service failure, unlike a retired epoch, whose 410 triggers a refresh;
   * `reconnecting` fails every live read of the thread's rows once they have loaded, which
   * Electric's client retries, so the rows stay on screen while the connection indicator reports
   * the outage; `catching-up` serves a stale view state that withholds segments on purpose. */
  sessionReplay?: "catching-up" | "unavailable" | "reconnecting";
  /** The harness was shut down and its runner feed ended while the Sandbox remains available, so the Thread can resume it. */
  endedAttachment?: boolean;
  /** Assistant output precedes coalesced queued input, then model/interrupt effects. */
  interleavedEvents?: boolean;
  /** Mundane lifecycle observations collapse into one comma-joined row; a prominent one (harness
   * lost) still stands alone and breaks the group around it. */
  lifecycleGroup?: boolean;
  threadSetup?: boolean;
  /** Shell tool calls as Claude and Codex record them, with a script and an output past their caps:
   * the model's description for Claude's Bash, the script without its `bash -lc` wrapper for Codex,
   * and a call still streaming its arguments. Opened, each is capped in height with its clipped
   * bottom to click for the rest. */
  shellCalls?: boolean;
  /** One assistant message containing a fenced code block, rendered by the shared code widget. */
  markdownCodeFence?: boolean;
  /** Put bidi, zero-width, and control characters in SSH arguments and output for marker review:
   * all should be readable as labeled CodeMirror markers in the specialized command/result preview. */
  hiddenCodepoints?: boolean;
  /** Interleave completed assistant text, folded tool/reasoning runs, and streaming assistant text.
   * Captured as the thread pane alone in the desktop viewport.
   * The history's "Jump to latest" control follows an IntersectionObserver
   * that reports a frame or two after the layout moves, so the scenario waits for the history to
   * say its layout has come to rest. */
  streamingInterleaved?: boolean;
  /** A reasoning step with no neighboring tool call, so `historyRows` never folds it into a run and
   * `EntityCard` renders it directly -- the standalone case, distinct from reasoning nested inside
   * a tool-run card. Collapsed it reads as one plain dimmed line with no card
   * chrome, like a collapsed run; opened it gets the card chrome (padding, border). */
  standaloneReasoning?: boolean;
  /** Put a fenced Python block in the standalone reasoning step to exercise its folded preview. */
  reasoningCodeFence?: boolean;
  /** An unfinished reasoning step in the running turn and another its turn left unfinished: the
   * title is blue and breathing while its turn runs (the animation is frozen in the sweep, so the
   * class is what says it is live), blue and still once its turn has ended. */
  unfinishedReasoning?: boolean;
  /** Give reasoning enough Markdown to exercise a clipped inline preview and disclosure. */
  longReasoningPreview?: boolean;
  /** Give an expanded reasoning disclosure enough body content to scroll past its original header. */
  longReasoningBody?: boolean;
  pendingCommands?: "mixed" | "controls" | "outcomes";
  /** Answer a command POST as the app does when a runner misses its admission deadline. Without
   * this it stays unanswered, like one queued behind the browser's connection limit. */
  commandAdmissionTimedOut?: boolean;
  /** Fail the Action group listing, which says whether a stored result is an MCP `CallToolResult`:
   * without the groups each result shows as its stored JSON. */
  actionGroupsUnavailable?: boolean;
  recovery?: "messages" | "tools" | "quiet";
  /** The idle thread's history ends in a failed turn, before or after the assistant's content. The
   * thread is named for it and carries it as its last completed turn, so its status mark is the turn
   * error's, in the sidebar and the topbar. */
  failedTurn?: "before-content" | "after-content";
}

// The JSON modules type string fields as `string`, not as the literal union `Scenario` names.
export const SCENARIOS: Record<string, Scenario> = { ...table, ...interactionTable } as Record<string, Scenario>;
