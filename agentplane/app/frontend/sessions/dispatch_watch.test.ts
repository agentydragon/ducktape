// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { CommandSchema, type Command } from "../../../proto/command_pb";
import { DeliveryState, type Dispatch } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { stubWatchFetch } from "../testing/watch";
import { dispatchFromJson } from "./client";
import { watchDispatches, watchRecentDispatchIssues, type RecentDispatchIssues } from "./dispatch_watch";
import { LocalCommands } from "./local_commands";

const SESSION = "session-1";
const COMMAND_TARGET = { sandbox: "box", sandboxUid: "20000000-0000-4000-8000-000000000001" };

let fetchMock: ReturnType<typeof stubWatchFetch>;
let stop: () => void = () => undefined;

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
  fetchMock = stubWatchFetch();
});

afterEach(() => {
  stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function queued(store: LocalCommands, commandId: string, legacyTarget?: typeof COMMAND_TARGET): Command {
  const command = create(CommandSchema, {
    commandId,
    operation: { case: "submitInput", value: { text: commandId } },
  });
  if (legacyTarget) {
    localStorage.setItem(
      `agentplane.pending:${SESSION}:${commandId}`,
      JSON.stringify({ command, submittedAt: 1, target: legacyTarget, admission: null })
    );
  } else {
    store.remember(command);
  }
  store.acknowledgeDispatch(
    command,
    dispatchFromJson({
      commandId,
      sessionId: SESSION,
      ...COMMAND_TARGET,
      state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
    })
  );
  return command;
}

function stateOf(store: LocalCommands, commandId: string): Dispatch["state"] | undefined {
  return store.getSnapshot().commands.find((value) => value.command.commandId === commandId)?.dispatch?.state;
}

// Dispatch frames are ProtoJSON as emitted by the gRPC-Gateway, with an opaque per-frame cursor.
function dispatchFrame(commandId: string, state: string, resumeToken: string, updatedAt = "2026-10-19T12:00:00Z") {
  return {
    dispatch: { commandId, sessionId: SESSION, ...COMMAND_TARGET, state, updatedAt },
    resumeToken,
  };
}

function caughtUp(resumeToken: string) {
  return { caughtUp: {}, resumeToken };
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

it("replaces a recent issues snapshot after resync and keeps the coverage warning", async () => {
  let latest: RecentDispatchIssues | undefined;
  stop = watchRecentDispatchIssues(SESSION, (state) => {
    latest = state;
  });
  await settle();
  const [first] = fetchMock.served;
  expect(first.url.searchParams.get("filter.sessionId")).toBe(SESSION);
  expect(first.url.searchParams.has("filter.recentIssueLimit")).toBe(true);
  const receipt = {
    commandId: "deleted-command",
    sessionId: SESSION,
    state: "DELIVERY_STATE_UNKNOWN",
    reason: "sandbox_deleted",
  };
  const decoded = { ...receipt, state: DeliveryState.UNKNOWN };
  first.send({ recentIssues: { receipts: [receipt], moreAvailable: true }, resumeToken: "snapshot" });
  await settle();
  expect(latest).toMatchObject({ connection: "current", receipts: [decoded], moreAvailable: true });
  first.end();
  await settle();
  expect(latest).toMatchObject({ connection: "stale", receipts: [decoded] });

  await vi.advanceTimersByTimeAsync(1_000);
  const [, resumed] = fetchMock.served;
  expect(resumed.url.searchParams.get("resumeToken")).toBe("snapshot");
  resumed.write('{"error":{"code":11,"message":"dispatch deletion coverage expired"}}\n');
  await settle();
  await vi.advanceTimersByTimeAsync(2_000);
  const [, , fresh] = fetchMock.served;
  expect(fresh.url.searchParams.has("resumeToken")).toBe(false);
  fresh.send({ recentIssues: {}, resumeToken: "replacement" });
  await settle();
  expect(latest).toMatchObject({ connection: "current", receipts: [], moreAvailable: false, coverageLost: true });
});

it("sorts and deduplicates the filter, applies its snapshot, and reuses the cursor after recreation", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  queued(store, "b");
  stop = watchDispatches(store, SESSION, ["b", "a", "a"]);
  await settle();
  const [watch] = fetchMock.served;
  watch.send(dispatchFrame("a", "DELIVERY_STATE_RUNNER_ADMITTED", "snapshot-row-a"));
  watch.send(dispatchFrame("b", "DELIVERY_STATE_QUEUED_IN_DISPATCHER", "snapshot-row-b"));
  watch.send(caughtUp("snapshot-checkpoint"));
  await settle();

  expect(watch.url.pathname).toBe("/upstream/dispatcher/v1/dispatches:watch");
  expect(watch.url.searchParams.get("filter.sessionId")).toBe(SESSION);
  expect(watch.url.searchParams.getAll("filter.commandIds")).toEqual(["a", "b"]);
  expect(watch.url.searchParams.has("resumeToken")).toBe(false);
  expect(stateOf(store, "a")).toBe(DeliveryState.RUNNER_ADMITTED);

  stop();
  stop = watchDispatches(store, SESSION, ["a", "b", "a"]);
  await settle();
  expect(fetchMock.served[1].url.searchParams.get("resumeToken")).toBe("snapshot-checkpoint");
  fetchMock.served[1].send(caughtUp("next-checkpoint"));
  await settle();
  expect(stateOf(store, "b")).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
});

it("marks an absent command unknown only after a complete fresh snapshot", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "kept");
  queued(store, "deleted");
  stop = watchDispatches(store, SESSION, ["kept", "deleted"]);
  await settle();
  const [watch] = fetchMock.served;
  watch.send(dispatchFrame("kept", "DELIVERY_STATE_QUEUED_IN_DISPATCHER", "row-kept"));
  await settle();
  expect(stateOf(store, "deleted")).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
  watch.send(caughtUp("fresh-checkpoint"));
  await settle();

  expect(stateOf(store, "deleted")).toBe(DeliveryState.UNKNOWN);
  expect(stateOf(store, "kept")).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
});

it("does not infer deletion from a resumed or recurring CaughtUp checkpoint", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "unchanged");
  store.saveDispatchWatchResumeToken(SESSION, ["unchanged"], "previous-checkpoint");
  stop = watchDispatches(store, SESSION, ["unchanged"]);
  await settle();
  const [watch] = fetchMock.served;
  watch.send(caughtUp("resumed-checkpoint"));
  watch.send(caughtUp("recurring-checkpoint"));
  await settle();

  expect(stateOf(store, "unchanged")).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
  expect(watch.url.searchParams.get("resumeToken")).toBe("previous-checkpoint");
});

it.each(["DELIVERY_STATE_FAILED", "DELIVERY_STATE_CANCELLED_IN_DISPATCHER"])(
  "applies a changed terminal receipt to a legacy command without losing its original Sandbox address (%s)",
  async (state) => {
    const store = new LocalCommands(SESSION);
    queued(store, "deleted", COMMAND_TARGET);
    store.saveDispatchWatchResumeToken(SESSION, ["deleted"], "previous-checkpoint");
    stop = watchDispatches(store, SESSION, ["deleted"]);
    await settle();
    fetchMock.served[0].send({
      dispatch: {
        commandId: "deleted",
        sessionId: SESSION,
        state,
        reason: "sandbox_deleted",
        updatedAt: "2026-10-19T12:00:01Z",
      },
      resumeToken: "terminal-checkpoint",
    });
    await settle();

    expect(stateOf(store, "deleted")).toBe(
      state === "DELIVERY_STATE_FAILED" ? DeliveryState.FAILED : DeliveryState.CANCELLED_IN_DISPATCHER
    );
    expect(new LocalCommands(SESSION).getSnapshot().commands[0]?.destination).toEqual({
      kind: "reconcile-only",
      target: COMMAND_TARGET,
    });
    expect(fetchMock.served[0].url.searchParams.get("resumeToken")).toBe("previous-checkpoint");
  }
);

it("restarts a partial fresh snapshot from the beginning instead of resuming before CaughtUp", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  queued(store, "b");
  stop = watchDispatches(store, SESSION, ["a", "b"]);
  await settle();
  fetchMock.served[0].send(dispatchFrame("a", "DELIVERY_STATE_RUNNER_ADMITTED", "partial-snapshot-cursor"));
  fetchMock.served[0].end();
  await settle();
  await vi.advanceTimersByTimeAsync(1_000);

  expect(fetchMock.served[1].url.searchParams.has("resumeToken")).toBe(false);
  expect(stateOf(store, "a")).toBe(DeliveryState.RUNNER_ADMITTED);
});

it.each([false, true])("times out before the initial checkpoint with partial rows: %s", async (partialRows) => {
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  const [first] = fetchMock.served;
  if (partialRows) first.send(dispatchFrame("a", "DELIVERY_STATE_QUEUED_IN_DISPATCHER", "partial-row"));

  await vi.advanceTimersByTimeAsync(59_999);
  expect(store.getSnapshot().dispatchWatchError).toBeNull();
  expect(first.signal?.aborted).toBe(false);
  await vi.advanceTimersByTimeAsync(1);
  expect(first.signal?.aborted).toBe(true);
  expect(store.getSnapshot().dispatchWatchError).toBe("stream did not reach its initial checkpoint");

  await vi.advanceTimersByTimeAsync(1_000);
  const retry = fetchMock.served[1];
  expect(retry.url.searchParams.has("resumeToken")).toBe(false);
  retry.send(dispatchFrame("a", "DELIVERY_STATE_QUEUED_IN_DISPATCHER", "retried-row"));
  retry.send(caughtUp("retried-checkpoint"));
  await vi.advanceTimersByTimeAsync(60_000);
  expect(retry.signal?.aborted).toBe(false);
  expect(store.getSnapshot().dispatchWatchError).toBeNull();
  expect(stateOf(store, "a")).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
});

it("suppresses a clean lease renewal that catches up promptly", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  store.saveDispatchWatchResumeToken(SESSION, ["a"], "previous-checkpoint");
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  fetchMock.served[0].send(caughtUp("initial-checkpoint"));
  await settle();

  fetchMock.served[0].end();
  await settle();
  expect(store.getSnapshot().dispatchWatchError).toBeNull();
  await vi.advanceTimersByTimeAsync(1_000);
  fetchMock.served[1].send(caughtUp("renewed-checkpoint"));
  await vi.advanceTimersByTimeAsync(3_000);
  expect(store.getSnapshot().dispatchWatchError).toBeNull();
});

it("warns after a prolonged clean disconnect and keeps the receipt through partial catch-up", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  const [first] = fetchMock.served;
  first.send(dispatchFrame("a", "DELIVERY_STATE_QUEUED_IN_DISPATCHER", "initial-row"));
  first.send(caughtUp("initial-checkpoint"));
  await settle();
  expect(store.getSnapshot().dispatchWatchError).toBeNull();

  first.end();
  await settle();
  expect(store.getSnapshot().dispatchWatchError).toBeNull();
  expect(stateOf(store, "a")).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
  await vi.advanceTimersByTimeAsync(1_000);
  const resumed = fetchMock.served[1];
  expect(resumed.url.searchParams.get("resumeToken")).toBe("initial-checkpoint");

  resumed.send(dispatchFrame("a", "DELIVERY_STATE_DELIVERING_TO_RUNNER", "resumed-row"));
  await vi.advanceTimersByTimeAsync(2_000);
  expect(store.getSnapshot().dispatchWatchError).toBe("The Dispatcher closed the stream.");
  expect(stateOf(store, "a")).toBe(DeliveryState.DELIVERING_TO_RUNNER);
  resumed.send(caughtUp("resumed-checkpoint"));
  await settle();
  expect(store.getSnapshot().dispatchWatchError).toBeNull();
  expect(store.dispatchWatchResumeToken(SESSION, ["a"])).toBe("resumed-checkpoint");
});

it("surfaces active-stream OUT_OF_RANGE, clears its cursor, and keeps the warning after resnapshot", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  const first = fetchMock.served[0];
  first.send(dispatchFrame("a", "DELIVERY_STATE_QUEUED_IN_DISPATCHER", "snapshot-row"));
  first.send(caughtUp("initial-checkpoint"));
  first.send(dispatchFrame("a", "DELIVERY_STATE_DELIVERING_TO_RUNNER", "advanced-checkpoint"));
  first.send(caughtUp("caught-up-checkpoint"));
  first.write('{"error":{"code":11,"message":"dispatch deletion coverage expired"}}\n');
  await settle();
  await vi.advanceTimersByTimeAsync(1_000);

  const fresh = fetchMock.served[1];
  expect(first.url.searchParams.has("resumeToken")).toBe(false);
  expect(fresh.url.searchParams.has("resumeToken")).toBe(false);
  expect(store.getSnapshot().dispatchWatchError).toContain("dispatch deletion coverage expired");
  expect(store.getSnapshot().dispatchWatchCoverageLost).toBe(true);
  fresh.send(dispatchFrame("a", "DELIVERY_STATE_FAILED", "fresh-row", "2026-10-19T12:00:02Z"));
  fresh.send(caughtUp("fresh-checkpoint"));
  await settle();

  expect(stateOf(store, "a")).toBe(DeliveryState.FAILED);
  expect(store.getSnapshot().dispatchWatchError).toBeNull();
  expect(new LocalCommands(SESSION).getSnapshot().dispatchWatchCoverageLost).toBe(true);
  expect(store.dispatchWatchResumeToken(SESSION, ["a"])).toBe("fresh-checkpoint");
});

it("treats HTTP 400 with gRPC OUT_OF_RANGE code as lost coverage", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  store.saveDispatchWatchResumeToken(SESSION, ["a"], "expired-checkpoint");
  fetchMock.fetch.mockImplementationOnce(() =>
    Promise.resolve(
      Response.json({ error: { code: 11, message: "dispatch deletion coverage expired" } }, { status: 400 })
    )
  );
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  await vi.advanceTimersByTimeAsync(1_000);

  expect(fetchMock.served[0].url.searchParams.has("resumeToken")).toBe(false);
  expect(store.getSnapshot().dispatchWatchCoverageLost).toBe(true);
});

it("does not advance a saved cursor when persisting a changed receipt fails", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  store.saveDispatchWatchResumeToken(SESSION, ["a"], "durable-checkpoint");
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  vi.spyOn(localStorage, "setItem").mockImplementationOnce(() => {
    throw new DOMException("storage full", "QuotaExceededError");
  });
  fetchMock.served[0].send(dispatchFrame("a", "DELIVERY_STATE_FAILED", "must-not-advance"));
  await settle();
  await vi.advanceTimersByTimeAsync(1_000);

  expect(fetchMock.served[1].url.searchParams.get("resumeToken")).toBe("durable-checkpoint");
});

it("reconnects before a malformed receipt can advance the cursor", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  fetchMock.served[0].send({ dispatch: { commandId: "a" }, resumeToken: "malformed-row-cursor" });
  fetchMock.served[0].send(dispatchFrame("a", "DELIVERY_STATE_FAILED", "later-row-cursor"));
  await settle();
  await vi.advanceTimersByTimeAsync(1_000);

  expect(stateOf(store, "a")).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
  expect(fetchMock.served[1].url.searchParams.has("resumeToken")).toBe(false);
});
