// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { CommandSchema, type Command } from "../../../proto/command_pb";
import { DeliveryState } from "../../../runner_gateway/dispatcher/session_dispatcher_pb";
import { stubWatchFetch } from "../testing/watch";
import { dispatchFromJson, type DispatchReceipt } from "./client";
import { watchDispatches } from "./dispatch_watch";
import { LocalCommands } from "./local_commands";

const SESSION = "session-1";

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

function queued(store: LocalCommands, commandId: string): Command {
  const command = create(CommandSchema, {
    commandId,
    operation: { case: "submitInput", value: { text: commandId } },
  });
  store.remember(command);
  store.acknowledgeDispatch(
    command,
    dispatchFromJson({
      commandId,
      sessionId: SESSION,
      state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
    })
  );
  return command;
}

function stateOf(store: LocalCommands, commandId: string): DispatchReceipt["state"] | undefined {
  return store.getSnapshot().commands.find((value) => value.command.commandId === commandId)?.dispatch?.state;
}

// A Dispatch as gRPC-Gateway writes it: proto-JSON that leaves out the unset `reason` and `admission`.
function dispatchFrame(commandId: string, state: string | number) {
  return { dispatch: { commandId, sessionId: SESSION, sandbox: "box", state, updatedAt: "2026-10-19T12:00:00Z" } };
}

function typedDispatchFrame(commandId: string) {
  return {
    dispatch: {
      commandId,
      sessionId: SESSION,
      sandbox: "box",
      sandboxUid: "box-incarnation",
      operation: "OPERATION_SUBMIT_INPUT",
      state: "DELIVERY_STATE_RUNNER_ADMITTED",
      updatedAt: "2026-10-19T12:00:00Z",
      admission: {
        cursor: "9007199254740993",
        origin: { sourceId: "runner", sequence: "9007199254740993" },
        event: { commandAdmitted: { command: { commandId, submitInput: { text: commandId } } } },
      },
    },
  };
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

it("asks for the Session's named commands and applies the rows it is sent", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  queued(store, "b");
  stop = watchDispatches(store, SESSION, ["a", "b"]);
  await settle();
  const [watch] = fetchMock.served;
  watch.send(dispatchFrame("a", "DELIVERY_STATE_RUNNER_ADMITTED"));
  watch.send({ caughtUp: {} });
  await settle();

  expect(watch.url.pathname).toBe("/upstream/dispatcher/v1/dispatches:watch");
  expect(watch.url.searchParams.get("filter.sessionId")).toBe(SESSION);
  expect(watch.url.searchParams.getAll("filter.commandIds")).toEqual(["a", "b"]);
  expect(stateOf(store, "a")).toBe(DeliveryState.RUNNER_ADMITTED);
  expect(
    store.getSnapshot().commands.find((value) => value.command.commandId === "a")?.dispatch?.updatedAt
  ).toBeDefined();
});

it("calls a command it holds no row for unknown once the watch has caught up, as after Sandbox deletion", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "kept");
  queued(store, "deleted");
  stop = watchDispatches(store, SESSION, ["kept", "deleted"]);
  await settle();
  const [watch] = fetchMock.served;
  watch.send(dispatchFrame("kept", "DELIVERY_STATE_QUEUED_IN_DISPATCHER"));
  await settle();
  expect(stateOf(store, "deleted")).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
  watch.send({ caughtUp: {} });
  await settle();

  expect(stateOf(store, "deleted")).toBe(DeliveryState.UNKNOWN);
  expect(stateOf(store, "kept")).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
});

it("judges what is missing by the rows of the current connection, not an earlier one", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  fetchMock.served[0].send(dispatchFrame("a", "DELIVERY_STATE_QUEUED_IN_DISPATCHER"));
  fetchMock.served[0].send({ caughtUp: {} });
  fetchMock.served[0].end();
  await vi.advanceTimersByTimeAsync(1_000);
  // The Sandbox was deleted while the page was disconnected.
  fetchMock.served[1].send({ caughtUp: {} });
  await settle();

  expect(stateOf(store, "a")).toBe(DeliveryState.UNKNOWN);
});

it("decodes a generated ProtoJSON frame including a large int64 admission cursor", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  fetchMock.served[0].send(typedDispatchFrame("a"));
  await settle();

  const admission = store.getSnapshot().commands.find((value) => value.command.commandId === "a")?.dispatch?.admission;
  expect(admission?.cursor).toBe(9007199254740993n);
  expect(admission?.event?.observation.case).toBe("commandAdmitted");
});

it("preserves an unknown numeric delivery state as a named variant across local storage", async () => {
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  fetchMock.served[0].send(dispatchFrame("a", 999));
  await settle();

  expect(stateOf(store, "a")).toEqual({ unknown: 999 });
  expect(store.getSnapshot().commands.find((value) => value.command.commandId === "a")?.dispatch?.reason).toBe(
    "Unrecognized delivery state 999"
  );
  const restored = new LocalCommands(SESSION);
  expect(stateOf(restored, "a")).toEqual({ unknown: 999 });
});

it("does not apply a later frame from a connection after a malformed or unknown-enum frame", async () => {
  const warning = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  fetchMock.served[0].send(dispatchFrame("a", "DELIVERY_STATE_FUTURE_VALUE"));
  fetchMock.served[0].send(dispatchFrame("a", "DELIVERY_STATE_FAILED"));
  await settle();

  expect(stateOf(store, "a")).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
  expect(warning).toHaveBeenCalledWith(
    "Session Dispatcher watch is reconnecting after a read error",
    expect.any(String)
  );
});

it.each([
  ["a missing frame oneof", {}],
  ["conflicting frame oneof members", { dispatch: { commandId: "a", sessionId: SESSION }, caughtUp: {} }],
  ["a Dispatch with no service-required state", { dispatch: { commandId: "a", sessionId: SESSION } }],
])("rejects %s before updating local state", async (_label, frame) => {
  const warning = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  fetchMock.served[0].send(frame);
  await settle();

  expect(stateOf(store, "a")).toBe(DeliveryState.QUEUED_IN_DISPATCHER);
  expect(warning).toHaveBeenCalledWith(
    "Session Dispatcher watch is reconnecting after a read error",
    expect.any(String)
  );
});
