// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { CommandSchema, type Command } from "../../../proto/command_pb";
import { stubWatchFetch } from "../testing/watch";
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
});

function queued(store: LocalCommands, commandId: string): Command {
  const command = create(CommandSchema, {
    commandId,
    operation: { case: "submitInput", value: { text: commandId } },
  });
  store.remember(command);
  store.acknowledgeDispatch(command, {
    commandId,
    sessionId: SESSION,
    state: "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
    admission: null,
  });
  return command;
}

function stateOf(store: LocalCommands, commandId: string): string | undefined {
  return store.getSnapshot().commands.find((value) => value.command.commandId === commandId)?.dispatch?.state;
}

// A Dispatch as gRPC-Gateway writes it: proto-JSON that leaves out the unset `reason` and `admission`.
function dispatchFrame(commandId: string, state: string) {
  return { dispatch: { commandId, sessionId: SESSION, sandbox: "box", state, updatedAt: "2026-10-19T12:00:00Z" } };
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
  expect(stateOf(store, "a")).toBe("DELIVERY_STATE_RUNNER_ADMITTED");
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
  expect(stateOf(store, "deleted")).toBe("DELIVERY_STATE_QUEUED_IN_DISPATCHER");
  watch.send({ caughtUp: {} });
  await settle();

  expect(stateOf(store, "deleted")).toBe("DELIVERY_STATE_UNKNOWN");
  expect(stateOf(store, "kept")).toBe("DELIVERY_STATE_QUEUED_IN_DISPATCHER");
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

  expect(stateOf(store, "a")).toBe("DELIVERY_STATE_UNKNOWN");
});

it("keeps applying frames after one it cannot read", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const store = new LocalCommands(SESSION);
  queued(store, "a");
  stop = watchDispatches(store, SESSION, ["a"]);
  await settle();
  fetchMock.served[0].send({ dispatch: { commandId: "a" } });
  fetchMock.served[0].send(dispatchFrame("a", "DELIVERY_STATE_FAILED"));
  await settle();

  expect(stateOf(store, "a")).toBe("DELIVERY_STATE_FAILED");
});
