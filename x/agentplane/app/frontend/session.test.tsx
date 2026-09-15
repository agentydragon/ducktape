// @vitest-environment happy-dom

import { create, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { command, getThread, models, type ThreadView } from "./client";
import { CommandSchema, type Command } from "../../protocol/command_pb";
import { EventSchema, ItemKind, TurnStatus } from "../../protocol/event_pb";
import { EventEntrySchema } from "../../protocol/event_log_pb";
import { AttachedSchema, Harness } from "../../runner/protocol_pb";
import { SessionView } from "./session";

vi.mock("./client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./client")>()),
  command: vi.fn(),
  getThread: vi.fn(),
  models: vi.fn(),
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];
const THREAD: ThreadView = {
  id: "10000000-0000-4000-8000-000000000001",
  sandbox: "composer-test",
  session_id: "session-test",
  harness: "HARNESS_CLAUDE",
  model: "test-model",
  cwd: "/test-workspace",
  created_at: "2026-01-01T00:00:00Z",
  name: "Test conversation",
  archived: false,
  last_cursor: 0,
  last_event_at: null,
  harness_state: "HARNESS_STATE_RUNNING",
};

beforeEach(() => localStorage.clear());

afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.resetAllMocks();
});

function event(cursor: number, observation: MessageInitShape<typeof EventSchema>["observation"]): string {
  return JSON.stringify(
    toJson(
      EventEntrySchema,
      create(EventEntrySchema, {
        cursor: BigInt(cursor),
        origin: { sourceId: "test-runner", sequence: BigInt(cursor) },
        event: create(EventSchema, { observation }),
      })
    )
  );
}

async function render(
  thread: ThreadView = THREAD,
  catalog: { HARNESS_CLAUDE: string[]; HARNESS_CODEX: string[] } = {
    HARNESS_CLAUDE: ["test-model"],
    HARNESS_CODEX: ["test-model"],
  }
): Promise<{
  container: HTMLDivElement;
  composer: HTMLTextAreaElement;
  stream: EventTarget;
  streams: EventTarget[];
  rerender: (threadId: string) => Promise<void>;
}> {
  const streams: EventTarget[] = [];
  vi.mocked(getThread).mockImplementation(async (id) => ({ ...thread, id }));
  vi.mocked(models).mockResolvedValue(catalog as never);
  vi.stubGlobal(
    "EventSource",
    class extends EventTarget {
      constructor(url: string) {
        super();
        if (url === "/live/sandboxes") {
          queueMicrotask(() =>
            this.dispatchEvent(
              new MessageEvent("snapshot", {
                data: JSON.stringify({
                  sandboxes: [{ name: "composer-test", state: "running" }],
                  watch: { fresh: true, stale_after_seconds: 90, refreshed_seconds_ago: { sandboxes: 0 } },
                }),
              })
            )
          );
        } else {
          streams.push(this);
        }
      }
      close(): void {}
    }
  );
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  async function rerender(threadId: string): Promise<void> {
    await act(async () => {
      root.render(
        <MantineProvider>
          <MemoryRouter>
            <SessionView threadId={threadId} onBack={() => {}} />
          </MemoryRouter>
        </MantineProvider>
      );
    });
  }
  await rerender(THREAD.id);
  const stream = streams[0];
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("attached", {
        data: JSON.stringify({
          sessionId: "session-test",
          spec: { harness: "HARNESS_CLAUDE", model: "test-model" },
        }),
      })
    );
    await Promise.resolve();
  });
  await act(async () => {
    stream.dispatchEvent(new MessageEvent("event", { data: event(1, { case: "harnessStarted", value: {} }) }));
  });
  const composer = container.querySelector("textarea");
  if (!composer) throw new Error("Missing composer");
  return { container, composer, stream, streams, rerender };
}

async function type(composer: HTMLTextAreaElement, text: string): Promise<void> {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(composer, text);
    composer.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function enter(composer: HTMLTextAreaElement): void {
  composer.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
}

function admission(value: Command, cursor = 2n) {
  return create(EventEntrySchema, {
    cursor,
    origin: { sourceId: "test-runner", sequence: cursor },
    event: { observation: { case: "commandAdmitted", value: { command: value } } },
  });
}

function retry(container: HTMLDivElement): void {
  const button = [...container.querySelectorAll("button")].find((button) => button.textContent === "Retry");
  if (!button) throw new Error("Missing Retry");
  button.click();
}

it("retains input before HTTP, suppresses repeated Enter, and allows another command before admission", async () => {
  let acknowledge = (): void => {
    throw new Error("Acknowledgment is not initialized");
  };
  vi.mocked(command)
    .mockImplementationOnce((_thread, value) => {
      expect(localStorage.length).toBe(1);
      return new Promise((resolve) => {
        acknowledge = () => resolve(admission(value));
      });
    })
    .mockImplementation(async (_thread, value) => admission(value, 3n));
  const { container, composer } = await render();
  await type(composer, "hello");
  await act(async () => {
    enter(composer);
    enter(composer);
  });
  expect(command).toHaveBeenCalledOnce();
  expect(container.textContent).toContain("Awaiting saved confirmation");
  expect(composer.disabled).toBe(false);
  expect(composer.value).toBe("");
  expect(container.querySelector(".agentplane-user-bubble")).toBeNull();
  await type(composer, "second message");
  await act(async () => enter(composer));
  expect(command).toHaveBeenCalledTimes(2);
  await act(async () => acknowledge());
  expect(container.textContent).toContain("Saved · replay catching up");
  expect(composer.disabled).toBe(false);
  expect(composer.value).toBe("");
});

it("restores unconfirmed input after remount and retries the exact command", async () => {
  vi.mocked(command)
    .mockRejectedValueOnce(new Error("response lost"))
    .mockImplementation(async (_thread, value) => admission(value));
  const { container, composer } = await render();
  await type(composer, "hello");
  await act(async () => enter(composer));
  expect(container.textContent).toContain("response lost");
  expect(composer.value).toBe("");
  expect(composer.disabled).toBe(false);
  const old = mounted.pop();
  if (!old) throw new Error("Missing mounted page");
  await act(async () => old.root.unmount());
  old.container.remove();
  const restored = await render();
  expect(restored.container.textContent).toContain("hello");
  expect(restored.container.textContent).toContain("Awaiting saved confirmation");
  expect(command).toHaveBeenCalledOnce();
  await act(async () => retry(restored.container));
  expect(vi.mocked(command).mock.calls[1]).toEqual(vi.mocked(command).mock.calls[0]);
  await type(restored.composer, "hello");
  await act(async () => enter(restored.composer));
  expect(vi.mocked(command).mock.calls[2]?.[1].commandId).not.toBe(vi.mocked(command).mock.calls[0]?.[1].commandId);
});

it("gives edited text a new input id after a failure", async () => {
  vi.mocked(command)
    .mockRejectedValueOnce(new Error("response lost"))
    .mockImplementation(async (_thread, value) => admission(value));
  const { composer } = await render();
  await type(composer, "hello");
  await act(async () => enter(composer));
  await type(composer, "different message");
  await act(async () => enter(composer));
  expect(vi.mocked(command).mock.calls[1]?.[1].commandId).not.toBe(vi.mocked(command).mock.calls[0]?.[1].commandId);
  expect(toJson(CommandSchema, vi.mocked(command).mock.calls[1]?.[1] as never)).toMatchObject({
    submitInput: { text: "different message" },
  });
});

it("preserves draft and sends nothing if local retention fails", async () => {
  const { container, composer } = await render();
  vi.spyOn(localStorage, "setItem").mockImplementationOnce(() => {
    throw new Error("storage full");
  });
  await type(composer, "do not lose this");
  await act(async () => enter(composer));
  expect(command).not.toHaveBeenCalled();
  expect(composer.value).toBe("do not lose this");
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("storage full");
});

it("does not regress streamed admission when the HTTP reply is lost", async () => {
  let fail = (_error: Error): void => {
    throw new Error("Missing request");
  };
  vi.mocked(command).mockImplementation(
    () =>
      new Promise((_resolve, reject) => {
        fail = reject;
      })
  );
  const { container, composer, stream } = await render();
  await type(composer, "saved input");
  await act(async () => enter(composer));
  const value = vi.mocked(command).mock.calls[0][1];
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "commandAdmitted", value: { command: value } }) })
    );
  });
  expect(container.textContent).toContain("Saved · awaiting effect");
  await act(async () => fail(new Error("lost reply")));
  expect(container.textContent).not.toContain("lost reply");
  expect(container.textContent).not.toContain("Awaiting saved confirmation");
  expect(localStorage.length).toBe(0);
  expect(container.querySelector(".agentplane-user-bubble")).toBeNull();
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(3, { case: "turnStarted", value: { turnId: "turn" } }) })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(4, {
          case: "harnessUserMessageConfirmed",
          value: {
            harnessMessageId: "native",
            turnId: "turn",
            text: "saved input",
            originCommandIds: [value.commandId],
          },
        }),
      })
    );
  });
  expect(container.querySelectorAll(".agentplane-user-bubble")).toHaveLength(1);
  expect(container.querySelector('[aria-label="Pending commands"]')).toBeNull();
});

it("keeps replayed admission authoritative when local cleanup fails and storage changes", async () => {
  vi.mocked(command).mockRejectedValue(new Error("lost reply"));
  const { container, composer, stream } = await render();
  await type(composer, "already saved");
  await act(async () => enter(composer));
  const value = vi.mocked(command).mock.calls[0][1];
  vi.spyOn(localStorage, "removeItem").mockImplementation(() => {
    throw new Error("local cleanup failed");
  });
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "commandAdmitted", value: { command: value } }) })
    );
  });
  await act(async () => window.dispatchEvent(new StorageEvent("storage", { key: null })));
  expect(localStorage.length).toBe(1);
  expect(container.textContent).toContain("Saved · awaiting effect");
  expect(container.textContent).not.toContain("Awaiting saved confirmation");
  expect(container.textContent).not.toContain("lost reply");
  expect([...container.querySelectorAll("button")].some((button) => button.textContent === "Retry")).toBe(false);
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("local cleanup failed");
});

it("does not advance replay from a later HTTP admission or skip intervening assistant output", async () => {
  vi.mocked(command).mockImplementation(async (_thread, value) => admission(value, 5n));
  const { container, composer, stream } = await render();
  await type(composer, "queued input");
  await act(async () => enter(composer));
  const value = vi.mocked(command).mock.calls[0][1];
  expect(container.textContent).toContain("Saved · replay catching up");
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "turnStarted", value: { turnId: "turn" } }) })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(3, { case: "itemStarted", value: { itemId: "answer", kind: ItemKind.ASSISTANT_TEXT } }),
      })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(4, { case: "textDelta", value: { itemId: "answer", text: "intervening output" } }),
      })
    );
    stream.dispatchEvent(
      new MessageEvent("event", { data: JSON.stringify(toJson(EventEntrySchema, admission(value, 5n))) })
    );
  });
  expect(container.textContent).toContain("intervening output");
  expect(container.textContent).toContain("Saved · awaiting effect");
  expect(container.querySelector('[role="alert"]')).toBeNull();
});

it("keeps targeted interrupt pending after admission and removes it only on the causal stopped turn", async () => {
  vi.mocked(command).mockImplementation(async (_thread, value) => admission(value, 3n));
  const { container, composer, stream } = await render();
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "turnStarted", value: { turnId: "clicked-turn" } }) })
    );
  });
  const interrupt = container.querySelector<HTMLButtonElement>('button[aria-label="Interrupt"]');
  if (!interrupt) throw new Error("Missing Interrupt");
  await act(async () => interrupt.click());
  const value = vi.mocked(command).mock.calls[0][1];
  expect(value.operation).toMatchObject({ case: "interruptTurn", value: { turnId: "clicked-turn" } });
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: JSON.stringify(toJson(EventEntrySchema, admission(value, 3n))) })
    );
  });
  expect(container.textContent).toContain("Interrupt turn clicked-turn");
  expect(container.textContent).toContain("Saved · awaiting effect");
  expect(composer.disabled).toBe(false);
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(4, {
          case: "turnCompleted",
          value: { turnId: "clicked-turn", status: TurnStatus.INTERRUPTED, interruptedByCommandId: value.commandId },
        }),
      })
    );
  });
  expect(container.querySelector('[aria-label="Pending commands"]')).toBeNull();
  expect(container.textContent).toContain("INTERRUPTED");
});

it("keeps the applied model until its causal effect while allowing the next input", async () => {
  vi.mocked(command).mockImplementation(async (_thread, value) =>
    admission(value, value.operation.case === "changeModel" ? 2n : 3n)
  );
  const { container, composer, stream } = await render(THREAD, {
    HARNESS_CLAUDE: ["test-model", "next"],
    HARNESS_CODEX: [],
  });
  const picker = container.querySelector<HTMLInputElement>('input[aria-label="Model"]');
  if (!picker) throw new Error("Missing Model");
  await act(async () => picker.click());
  const option = [...document.querySelectorAll<HTMLElement>('[role="option"]')].find(
    (value) => value.textContent === "next"
  );
  if (!option) throw new Error("Missing next model option");
  await act(async () => option.click());
  const model = vi.mocked(command).mock.calls[0][1];
  expect(model.operation).toMatchObject({ case: "changeModel", value: { model: "next" } });
  await act(async () =>
    stream.dispatchEvent(
      new MessageEvent("event", { data: JSON.stringify(toJson(EventEntrySchema, admission(model))) })
    )
  );
  expect(picker.value).toBe("test-model");
  expect(container.textContent).toContain("Change model to next");
  expect(container.textContent).toContain("Saved · awaiting effect");
  await type(composer, "apply it on this turn");
  await act(async () => enter(composer));
  expect(command).toHaveBeenCalledTimes(2);
  const input = vi.mocked(command).mock.calls[1][1];
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: JSON.stringify(toJson(EventEntrySchema, admission(input, 3n))) })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(4, { case: "modelChanged", value: { model: "next", commandId: model.commandId } }),
      })
    );
  });
  expect(picker.value).toBe("next");
  expect(container.textContent).not.toContain("Change model to next");
  expect(container.textContent).toContain("apply it on this turn");
});

it("shows a failed model command as a command outcome, not a user message", async () => {
  const { container, stream } = await render();
  const model = create(CommandSchema, {
    commandId: "model",
    operation: { case: "changeModel", value: { model: "unsupported" } },
  });
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: JSON.stringify(toJson(EventEntrySchema, admission(model))) })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(3, { case: "commandFailed", value: { commandId: "model", reason: "model unavailable" } }),
      })
    );
  });
  expect(container.querySelector('[aria-label="Command outcomes"]')?.textContent).toContain(
    "Change model to unsupported"
  );
  expect(container.textContent).toContain("Failed: model unavailable");
  expect(container.querySelector(".agentplane-user-bubble")).toBeNull();
});

it("collapses a lone tool call behind its run disclosure", async () => {
  const { container, stream } = await render();
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "turnStarted", value: { turnId: "t1" } }) })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(3, {
          case: "itemStarted",
          value: { itemId: "tool#0", kind: ItemKind.TOOL_CALL, toolName: "Bash" },
        }),
      })
    );
    stream.dispatchEvent(
      new MessageEvent("event", {
        data: event(4, {
          case: "itemCompleted",
          value: { itemId: "tool#0", outcome: { case: "tool", value: { output: "ok", succeeded: true } } },
        }),
      })
    );
  });
  const control = [...container.querySelectorAll("button")].find((button) =>
    button.textContent?.includes("1 tool call")
  );
  expect(control?.getAttribute("aria-expanded")).toBe("false");
});

it("does not animate unfinished historical items after their turn or harness ends", async () => {
  const { container, stream } = await render();
  async function emit(cursor: number, observation: MessageInitShape<typeof EventSchema>["observation"]) {
    await act(async () => {
      stream.dispatchEvent(new MessageEvent("event", { data: event(cursor, observation) }));
    });
  }
  await emit(2, { case: "turnStarted", value: { turnId: "old-turn" } });
  await emit(3, { case: "itemStarted", value: { itemId: "old-item", kind: ItemKind.ASSISTANT_TEXT } });
  expect(container.querySelectorAll('[aria-label="Streaming"]')).toHaveLength(1);
  await emit(4, { case: "turnCompleted", value: { turnId: "old-turn", status: TurnStatus.INTERRUPTED } });
  await emit(5, { case: "turnStarted", value: { turnId: "new-turn" } });
  expect(container.querySelectorAll('[aria-label="Streaming"]')).toHaveLength(0);
  expect(container.querySelectorAll('[aria-label="Incomplete in retained history"]')).toHaveLength(1);
  await emit(6, { case: "itemStarted", value: { itemId: "new-item", kind: ItemKind.ASSISTANT_TEXT } });
  expect(container.querySelectorAll('[aria-label="Streaming"]')).toHaveLength(1);
  await emit(7, { case: "harnessExited", value: { exitCode: 0 } });
  expect(container.querySelectorAll('[aria-label="Streaming"]')).toHaveLength(0);
  expect(container.querySelectorAll('[aria-label="Incomplete in retained history"]')).toHaveLength(2);
});

it("adds Raw evidence without reordering conversation anchors or resetting an expanded tool run", async () => {
  const { container, stream } = await render();
  const observations: MessageInitShape<typeof EventSchema>["observation"][] = [
    { case: "turnStarted", value: { turnId: "turn" } },
    { case: "itemStarted", value: { itemId: "first-tool", kind: ItemKind.TOOL_CALL, toolName: "First tool" } },
    { case: "toolArguments", value: { itemId: "first-tool", argumentsJson: '{"command":"first"}' } },
    { case: "itemStarted", value: { itemId: "second-tool", kind: ItemKind.TOOL_CALL, toolName: "Second tool" } },
    {
      case: "harnessUserMessageConfirmed",
      value: { harnessMessageId: "interleaved", text: "Processed after those tools", turnId: "turn" },
    },
    { case: "itemStarted", value: { itemId: "reply", kind: ItemKind.ASSISTANT_TEXT } },
    { case: "textDelta", value: { itemId: "reply", text: "Current accumulated reply" } },
    { case: "modelChanged", value: { commandId: "model", model: "next" } },
    {
      case: "turnCompleted",
      value: { turnId: "turn", status: TurnStatus.INTERRUPTED, interruptedByCommandId: "stop" },
    },
  ];
  await act(async () => {
    observations.forEach((observation, index) => {
      stream.dispatchEvent(new MessageEvent("event", { data: event(index + 2, observation) }));
    });
  });
  const run = [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("2 tool calls"));
  if (!run) throw new Error("Missing tool disclosure");
  await act(async () => run.click());
  expect(run.getAttribute("aria-expanded")).toBe("true");
  const anchors = () =>
    [...container.querySelectorAll("[data-conversation-anchor]")].map((node) =>
      node.getAttribute("data-conversation-anchor")
    );
  const normalAnchors = anchors();
  expect(normalAnchors).toEqual(["2", "3", "6", "7", "9", "10"]);
  expect(container.querySelectorAll(".agentplane-user-bubble")).toHaveLength(1);
  const more = container.querySelector<HTMLButtonElement>('[aria-label="More"]');
  if (!more) throw new Error("Missing More menu");
  await act(async () => more.click());
  const raw = [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')].find((item) =>
    item.textContent?.includes("Raw frames")
  );
  if (!raw) throw new Error("Missing Raw frames toggle");
  await act(async () => raw.click());
  expect(anchors()).toEqual(normalAnchors);
  expect(run.getAttribute("aria-expanded")).toBe("true");
  expect(container.textContent).toContain("Current accumulated reply");
  expect(container.textContent).toContain("current aggregates through event 10");
  expect(container.textContent).toContain("harness message interleaved · first event 6");
  expect(container.querySelectorAll(".agentplane-user-bubble")).toHaveLength(1);
  expect(
    [...container.querySelectorAll("[data-event-cursor]")].map((node) => node.getAttribute("data-event-cursor"))
  ).toEqual(Array.from({ length: 10 }, (_, index) => String(index + 1)));
});

it("isolates transcript, draft, and late transport callbacks when the target changes", async () => {
  const { container, composer, stream, streams, rerender } = await render();
  await type(composer, "draft for the old target");
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "turnStarted", value: { turnId: "old-turn" } }) })
    );
  });
  await rerender("next-thread");
  const nextComposer = container.querySelector("textarea");
  expect(nextComposer?.value).toBe("");
  expect(nextComposer?.disabled).toBe(true);
  expect(container.textContent).not.toContain("old-turn");
  await act(async () => {
    stream.dispatchEvent(new MessageEvent("error", { data: "old target failure" }));
    streams[1].dispatchEvent(
      new MessageEvent("attached", {
        data: JSON.stringify({
          sessionId: "next-session",
          spec: { harness: "HARNESS_CLAUDE", model: "test-model" },
        }),
      })
    );
    streams[1].dispatchEvent(new MessageEvent("event", { data: event(1, { case: "harnessStarted", value: {} }) }));
    await Promise.resolve();
  });
  expect(container.textContent).not.toContain("old target failure");
  expect(nextComposer?.disabled).toBe(false);
  expect(container.querySelector('[role="alert"]')).toBeNull();
});

it("shows a replay integrity failure and stops controls at the verified prefix", async () => {
  const { container, composer, stream } = await render();
  await act(async () => {
    stream.dispatchEvent(new MessageEvent("event", { data: event(3, { case: "harnessStarted", value: {} }) }));
  });
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("Event gap: expected 2, received 3");
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("through event 1");
  expect(composer.disabled).toBe(true);
  expect(container.querySelector<HTMLInputElement>('input[aria-label="Model"]')?.placeholder).toBe("Model unavailable");
});

it("keeps the model unknown during catch-up instead of showing an older replayed model", async () => {
  const { container, composer, stream } = await render(THREAD, {
    HARNESS_CLAUDE: ["old", "current", "next"],
    HARNESS_CODEX: [],
  });
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("attached", {
        data: JSON.stringify(
          toJson(
            AttachedSchema,
            create(AttachedSchema, { lastCursor: 3n, spec: { harness: Harness.CLAUDE, model: "current" } })
          )
        ),
      })
    );
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(2, { case: "modelChanged", value: { model: "old" } }) })
    );
  });
  const picker = container.querySelector<HTMLInputElement>('input[aria-label="Model"]');
  expect(picker?.value).toBe("");
  expect(composer.disabled).toBe(true);
  expect(container.querySelector('[role="status"]')?.textContent).toBe("Catching up: 2 / 3 events");
  await act(async () => {
    stream.dispatchEvent(new MessageEvent("event", { data: event(3, { case: "harnessStarted", value: {} }) }));
  });
  expect(picker?.value).toBe("current");
  expect(composer.disabled).toBe(false);
  await act(async () => {
    stream.dispatchEvent(
      new MessageEvent("event", { data: event(4, { case: "modelChanged", value: { model: "next" } }) })
    );
  });
  expect(picker?.value).toBe("next");
});
