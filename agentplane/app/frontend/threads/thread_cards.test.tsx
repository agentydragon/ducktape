// @vitest-environment happy-dom

import { create, toJson, type MessageInitShape } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { EventSchema, ItemKind, RecoveryDisposition, TurnStatus } from "../../../protocol/event_pb";
import { RetainedDisclosureProvider } from "./retained_disclosures";
import { EntityCard } from "./thread_cards";
import { testItem } from "./thread_entity_fixture";
import { entity, reference, serving, toggle } from "./thread_state_fixture";
import { ThreadSyncContext, type ThreadEntity } from "./thread_sync";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];

afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount());
    container.remove();
  }
});

async function renderCard(card: ThreadEntity, bodies: Record<string, string>, live = false): Promise<HTMLDivElement> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <ThreadSyncContext.Provider value={serving(new Map(Object.entries(bodies)))}>
          <RetainedDisclosureProvider>
            {/* The history's row carries the anchor; a card renders inside it. */}
            <div data-thread-anchor={card.cursor.toString()}>
              <EntityCard threadId="test-thread" entity={card} live={live} />
            </div>
          </RetainedDisclosureProvider>
        </ThreadSyncContext.Provider>
      </MantineProvider>
    )
  );
  return container;
}

type Observation = MessageInitShape<typeof EventSchema>["observation"];

function renderLifecycle(observation: string, event: Observation): Promise<HTMLDivElement> {
  const state = { observation, event: toJson(EventSchema, create(EventSchema, { observation: event })) };
  return renderCard(entity("lifecycle", state, {}), {});
}

async function disclose(container: HTMLElement, summary: string): Promise<HTMLDetailsElement> {
  const control = [...container.querySelectorAll("summary")].find((element) => element.textContent === summary);
  if (!control) throw new Error(`no ${summary} disclosure`);
  const details = control.parentElement as HTMLDetailsElement;
  await act(async () => {
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
  });
  return details;
}

const PROSE = "Run **every** test\n- first";

describe("recovery presentation", () => {
  it.each([null, "text"])("labels retained text with completion %s", async (completion) => {
    const container = await renderCard(
      testItem(
        1,
        ItemKind.ASSISTANT_TEXT,
        { completion, recovery: RecoveryDisposition.RETAINED },
        { textRef: reference("test-entity-1", "text") }
      ),
      { "test-entity-1:text": "Remember the name in the margin" }
    );
    expect(container.textContent).toContain("Remember the name in the margin");
    expect(container.querySelector('[aria-label="Retained in context"]')).not.toBeNull();
  });

  it("collapses discarded text and preserves it behind a disclosure", async () => {
    const container = await renderCard(
      testItem(
        1,
        ItemKind.ASSISTANT_TEXT,
        { completion: null, recovery: RecoveryDisposition.ABSENT },
        { textRef: reference("test-entity-1", "text") }
      ),
      { "test-entity-1:text": "The seam widened." }
    );
    expect(container.textContent).not.toContain("The seam widened.");
    await disclose(container, "Discarded output (not retained in model context)");
    expect(container.textContent).toContain("The seam widened.");
    expect(container.querySelector('[aria-label="Not retained in context"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Interrupted"]')).not.toBeNull();
  });

  it("shows unknown retention and its reason without hiding the observed text", async () => {
    const container = await renderCard(
      testItem(
        1,
        ItemKind.ASSISTANT_TEXT,
        {
          completion: null,
          recovery: RecoveryDisposition.UNKNOWN,
          recovery_reason: "The harness history could not be inspected.",
        },
        { textRef: reference("test-entity-1", "text") }
      ),
      { "test-entity-1:text": "The bells were ringing." }
    );
    expect(container.textContent).toContain("The bells were ringing.");
    expect(container.textContent).toContain("The harness history could not be inspected.");
    expect(container.querySelector('[aria-label="Retention unknown"]')).not.toBeNull();
  });

  it("labels revised content as continuation, without inventing a tool outcome", async () => {
    const container = await renderCard(
      testItem(
        1,
        ItemKind.TOOL_CALL,
        {
          tool_name: "Bash",
          completion: null,
          tool_succeeded: null,
          recovery: RecoveryDisposition.REVISED,
        },
        { outputRef: reference("test-entity-1", "output") }
      ),
      { "test-entity-1:output": "aborted" }
    );
    expect(container.querySelector('[aria-label="Revised for continuation"]')).not.toBeNull();
    await toggle(container.querySelector("summary")!);
    expect(container.textContent).toContain("Recovery content does not establish a tool execution outcome.");
    expect(container.textContent).toContain("Continuation output");
    expect(container.textContent).toContain("aborted");
    expect(container.querySelector('[aria-label="Succeeded"]')).toBeNull();
    expect(container.querySelector('[aria-label="Failed"]')).toBeNull();
  });

  it("preserves a successful execution result when its context was discarded", async () => {
    const container = await renderCard(
      testItem(
        1,
        ItemKind.TOOL_CALL,
        {
          tool_name: "Bash",
          completion: "tool",
          tool_succeeded: true,
          recovery: RecoveryDisposition.ABSENT,
        },
        { outputRef: reference("test-entity-1", "output") }
      ),
      { "test-entity-1:output": "Created report.txt" }
    );
    await disclose(container, "Bash: discarded context (not retained in model context)");
    expect(container.textContent).toContain("does not undo tool side effects");
    expect(container.querySelector('[aria-label="Succeeded"]')).not.toBeNull();
    expect(container.textContent).toContain("Created report.txt");
  });
});

function toolEntity(toolName: string): ThreadEntity {
  return entity(
    "item",
    {
      kind: ItemKind.TOOL_CALL,
      tool_name: toolName,
      completion: "tool",
      tool_succeeded: true,
      recovery: null,
      recovery_reason: "",
    },
    { argumentsRef: reference("test-tool", "arguments"), outputRef: reference("test-tool", "output") }
  );
}

/** A completed tool call whose arguments and output are the given values. */
function renderTool(toolName: string, args: unknown, output = "test-output"): Promise<HTMLDivElement> {
  return renderCard(toolEntity(toolName), {
    "test-tool:arguments": JSON.stringify(args),
    "test-tool:output": output,
  });
}

const lineOf = (container: HTMLElement): string | undefined =>
  container.querySelector(".agentplane-step-preview")?.textContent ?? undefined;

describe("reasoning step marks", () => {
  const reasoningStep = (state: Partial<Extract<ThreadEntity["state"], { kind: number }>>): ThreadEntity =>
    entity(
      "item",
      {
        kind: ItemKind.REASONING,
        tool_name: "",
        completion: null,
        tool_succeeded: null,
        recovery: null,
        recovery_reason: "",
        ...state,
      },
      { textRef: reference("test-reasoning", "text") }
    );
  const bodies = { "test-reasoning:text": "Weighing the next step." };

  it.each([
    [true, "Streaming", true],
    [false, "Incomplete", false],
  ])(
    "marks an unfinished step (live: %s) by its title, with no badge row above the line",
    async (live, label, breathes) => {
      const container = await renderCard(reasoningStep({}), bodies, live);
      const title = container.querySelector(".agentplane-step-title")!;
      expect(title.textContent).toBe("Reasoning");
      expect(title.getAttribute("style")).toContain("blue");
      expect(title.getAttribute("title")).toBe(label);
      expect(title.classList.contains("agentplane-step-title--streaming")).toBe(breathes);
      expect(container.querySelector(`[aria-label="${label}"]`)).toBeNull();
    }
  );

  it("leaves a finished step in the dimmed title", async () => {
    const container = await renderCard(reasoningStep({ completion: "text" }), bodies);
    expect(container.querySelector(".agentplane-step-title")?.getAttribute("style")).not.toContain("blue");
  });

  it("leaves an interrupted step to its own badges, which the title has no mark for", async () => {
    const container = await renderCard(reasoningStep({ recovery: RecoveryDisposition.RETAINED }), bodies);
    expect(container.querySelector('[aria-label="Interrupted"]')).not.toBeNull();
    expect(container.querySelector(".agentplane-step-title")?.getAttribute("style")).not.toContain("blue");
  });
});

describe("tool call rows", () => {
  const COMMAND = "docker ps --all\n  --format '{{.Names}}'";

  it("folds a Claude Bash call to the line the model wrote for it, and draws nothing else", async () => {
    const container = await renderTool("Bash", { command: COMMAND, description: "List every container" });
    expect(container.querySelector(".agentplane-step-title")?.textContent).toBe("Bash");
    expect(lineOf(container)).toBe("List every container");
    expect(container.querySelector(".agentplane-code-block")).toBeNull();
    expect(container.textContent).not.toContain("test-output");
  });

  it("folds a Bash call with no description to its command on one line, as highlighted shell", async () => {
    const container = await renderTool("Bash", { command: `set -e\n${COMMAND}` });
    expect(lineOf(container)).toBe("set -e docker ps --all --format '{{.Names}}'");
    expect(
      container.querySelector(".agentplane-step-preview .agentplane-code-inline .agentplane-tok-keyword")?.textContent
    ).toBe("set");
  });

  it("does not highlight what the model wrote to say what a command is for", async () => {
    const container = await renderTool("Bash", { command: COMMAND, description: "List every container" });
    expect(container.querySelector(".agentplane-code-inline")).toBeNull();
  });

  it("folds Codex's command to the script it ran, without the shell that ran it", async () => {
    const container = await renderTool("commandExecution", {
      command: String.raw`/bin/bash -lc "echo \"test-script\""`,
      cwd: "/test-workspace",
    });
    expect(container.querySelector(".agentplane-step-title")?.textContent).toBe("Shell");
    expect(lineOf(container)).toBe('echo "test-script"');
  });

  it("opens to the command as shell and the output, with the model's reason and how it ran", async () => {
    const container = await renderTool(
      "Bash",
      { command: COMMAND, description: "List every container", timeout: 5000 },
      "test-container\n"
    );
    await toggle(container.querySelector("summary")!);
    expect(container.textContent).toContain("List every container");
    expect(container.textContent).toContain("Timeout 5000 ms");
    const [command, output] = [...container.querySelectorAll(".agentplane-code-block")].map((block) =>
      [...block.querySelectorAll(".cm-line")].map((line) => line.textContent ?? "").join("\n")
    );
    expect(command).toBe(COMMAND);
    expect(output).toBe("test-container");
    expect(container.textContent).not.toContain('"command"');
  });

  it("shows the JSON a command call holds in place of the command while Raw is on", async () => {
    const container = await renderTool("Bash", { command: "ls", description: "List files" });
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
    await toggle(container.querySelector("summary")!);
    // On the title line, outside its summary and apart from the content below it.
    const raw = container.querySelector<HTMLInputElement>(".agentplane-step-row > .agentplane-step-controls input")!;
    expect(raw.type).toBe("checkbox");
    const firstBlock = () => container.querySelector(".agentplane-code-block")?.textContent;
    expect(firstBlock()).toBe("ls");

    await act(async () => raw.click());
    expect(firstBlock()).toContain('"command":"ls"');
    expect(firstBlock()).toContain('"description":"List files"');

    await act(async () => raw.click());
    expect(firstBlock()).toBe("ls");
  });

  it("folds any other tool to its arguments' JSON, which is all it can open to", async () => {
    const container = await renderTool("Read", { file_path: "test-file" });
    expect(container.querySelector(".agentplane-step-title")?.textContent).toBe("Read");
    expect(lineOf(container)).toBe('{"file_path":"test-file"}');
    await toggle(container.querySelector("summary")!);
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
  });

  it("draws a Bash call it could not show whole as its JSON, with nothing to switch", async () => {
    const container = await renderTool("Bash", { command: "ls", test_extra: true });
    await toggle(container.querySelector("summary")!);
    expect(container.querySelector(".agentplane-code-block")?.textContent).toContain('"test_extra":true');
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
  });

  it("marks a failed call by its title in red while folded, and by a badge once opened", async () => {
    const container = await renderCard(
      entity(
        "item",
        {
          kind: ItemKind.TOOL_CALL,
          tool_name: "Bash",
          completion: "tool",
          tool_succeeded: false,
          recovery: null,
          recovery_reason: "",
        },
        { outputRef: reference("test-tool", "output") }
      ),
      { "test-tool:output": "test-failure" }
    );
    const title = container.querySelector(".agentplane-step-title")!;
    expect(title.getAttribute("style")).toContain("red");
    expect(title.getAttribute("title")).toBe("Failed");
    expect(container.querySelector('[aria-label="Failed"]')).toBeNull();

    await toggle(container.querySelector("summary")!);
    expect(container.querySelector(".agentplane-step-title")?.getAttribute("style")).not.toContain("red");
    expect(container.querySelector('[aria-label="Failed"]')).not.toBeNull();
  });

  it.each([
    [true, "Streaming", true],
    [false, "Incomplete", false],
  ])(
    "marks a call still unfinished (live: %s) by a blue title while folded, and by a badge once opened",
    async (live, label, breathes) => {
      const container = await renderCard(
        entity(
          "item",
          {
            kind: ItemKind.TOOL_CALL,
            tool_name: "Bash",
            completion: null,
            tool_succeeded: null,
            recovery: null,
            recovery_reason: "",
          },
          { outputRef: reference("test-tool", "output") }
        ),
        { "test-tool:output": "test-partial" },
        live
      );
      const title = () => container.querySelector(".agentplane-step-title")!;
      expect(title().getAttribute("style")).toContain("blue");
      expect(title().getAttribute("title")).toBe(label);
      // Only a call something is working on breathes.
      expect(title().classList.contains("agentplane-step-title--streaming")).toBe(breathes);
      expect(container.querySelector(`[aria-label="${label}"]`)).toBeNull();

      await toggle(container.querySelector("summary")!);
      expect(title().getAttribute("style")).not.toContain("blue");
      expect(title().classList.contains("agentplane-step-title--streaming")).toBe(false);
      expect(container.querySelector(`[aria-label="${label}"]`)).not.toBeNull();
    }
  );

  it("leaves a call that went well in the dimmed title every line has", async () => {
    const container = await renderTool("Bash", { command: "ls" });
    expect(container.querySelector(".agentplane-step-title")?.getAttribute("style")).not.toContain("red");
    expect(container.querySelector(".agentplane-step-title")?.getAttribute("title")).toBeNull();
  });

  it("keeps a call's open state and Raw where the reader left them when its row leaves the DOM", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    mounted.push({ root, container });
    const bodies = new Map([
      ["test-tool:arguments", JSON.stringify({ command: "ls" })],
      ["test-tool:output", "test-output"],
    ]);
    // One provider throughout, as the thread has: only the row mounts and unmounts.
    const show = (visible: boolean) =>
      act(async () =>
        root.render(
          <MantineProvider env="test">
            <ThreadSyncContext.Provider value={serving(bodies)}>
              <RetainedDisclosureProvider>
                {visible && <EntityCard threadId="test-thread" entity={toolEntity("Bash")} live={false} />}
              </RetainedDisclosureProvider>
            </ThreadSyncContext.Provider>
          </MantineProvider>
        )
      );
    await show(true);
    await toggle(container.querySelector("summary")!);
    await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());

    await show(false);
    expect(container.querySelector("details")).toBeNull();
    await show(true);
    expect(container.querySelector("details")?.open).toBe(true);
    expect(container.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(true);
  });
});

describe("EntityCard", () => {
  it("renders a tool call's JSON arguments highlighted and its plain output verbatim, both as code", async () => {
    const container = await renderTool("Read", { file_path: "test-file", limit: 30 }, PROSE);
    await toggle(container.querySelector("summary")!);

    const [args, output] = container.querySelectorAll(".agentplane-code-block");
    expect(args.querySelector(".cm-editor")).not.toBeNull();
    expect(args.textContent).toContain('"file_path":"test-file"');
    expect(args.textContent).toContain('"limit":30');
    expect([...output.querySelectorAll(".cm-line")].map((line) => line.textContent ?? "").join("\n")).toBe(PROSE);
    expect(output.querySelector("strong, li")).toBeNull();
  });

  it("renders the assistant's text as Markdown", async () => {
    const container = await renderCard(
      entity(
        "item",
        {
          kind: ItemKind.ASSISTANT_TEXT,
          tool_name: "",
          completion: PROSE,
          tool_succeeded: null,
          recovery: null,
          recovery_reason: "",
        },
        { textRef: reference("test-reply", "text") }
      ),
      { "test-reply:text": PROSE }
    );
    expect(container.querySelector(".agentplane-markdown strong")?.textContent).toBe("every");
    expect(container.querySelector(".agentplane-markdown li")?.textContent).toBe("first");
  });

  it("renders the operator's input as typed, not as Markdown", async () => {
    const container = await renderCard(
      entity(
        "confirmed_input",
        { harness_message_id: "test-message", origin_command_ids: [] },
        { inputRef: reference("test-message", "confirmed_input") }
      ),
      { "test-message:confirmed_input": PROSE }
    );
    expect(container.querySelector(".agentplane-user-bubble .agentplane-verbatim")?.textContent).toBe(PROSE);
    expect(container.querySelector(".agentplane-markdown, strong, li")).toBeNull();
  });

  it("renders a still-pending sent message as the same bubble, with its status beside it, not inside", async () => {
    const container = await renderCard(
      entity(
        "command",
        { operation: "submit_input", outcome: "pending", outcome_cursor: null, outcome_reason: null },
        { inputRef: reference("test-message", "command_input") }
      ),
      { "test-message:command_input": PROSE }
    );
    const bubble = container.querySelector<HTMLElement>(".agentplane-user-bubble");
    expect(bubble?.querySelector(".agentplane-verbatim")?.textContent).toBe(PROSE);
    // The bubble holds only the message, so it is the same once the status goes and nothing moves.
    expect(bubble?.textContent).toBe(PROSE);
    expect(bubble?.parentElement?.querySelector('[role="status"]')?.textContent).toBe("Saved · awaiting effect");
  });

  it.each<[string, Observation, string]>([
    [
      "turn_completed",
      { case: "turnCompleted", value: { turnId: "test-turn", status: TurnStatus.COMPLETED } },
      "Turn completed",
    ],
    [
      "turn_completed",
      { case: "turnCompleted", value: { turnId: "test-turn", status: TurnStatus.INTERRUPTED } },
      "Turn interrupted",
    ],
    ["turn_started", { case: "turnStarted", value: { turnId: "test-turn", model: "test-model" } }, "Turn started"],
    [
      "model_changed",
      { case: "modelChanged", value: { previousModel: "test-model", model: "test-model-next" } },
      "Model changed to test-model-next",
    ],
    [
      "reasoning_effort_changed",
      { case: "reasoningEffortChanged", value: { previousEffort: "low", effort: "high" } },
      "Reasoning effort changed to high",
    ],
    ["harness_exited", { case: "harnessExited", value: { exitCode: 3 } }, "Harness exited with code 3"],
  ])("shows an ordinary %s as one line, its raw event behind the row's Evidence", async (observation, event, line) => {
    const container = await renderLifecycle(observation, event);
    const row = container.querySelector('[data-thread-anchor="1"]')!;
    expect(row.querySelector('button[aria-label="Evidence"]')).not.toBeNull();
    expect(row.textContent).toBe(line);
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it.each(['Test API failure: HTTP 429\n<img src="x" onerror="throw new Error()">', ""])(
    "shows a failed turn's error as a plain-text alert: %j",
    async (error) => {
      const container = await renderLifecycle("turn_completed", {
        case: "turnCompleted",
        value: { turnId: "test-failed-turn", status: TurnStatus.FAILED, error },
      });
      const alert = container.querySelector('[data-thread-anchor="1"] [role="alert"]')!;
      expect(alert.textContent).toBe(`Turn failed ${error || "The harness reported no error details."}`);
      expect(alert.querySelector("img")).toBeNull();
    }
  );

  it("shows an interrupted turn's error dimmed beneath its line, not as an alert", async () => {
    const container = await renderLifecycle("turn_completed", {
      case: "turnCompleted",
      value: { turnId: "test-turn", status: TurnStatus.INTERRUPTED, error: "test interrupt detail" },
    });
    expect(container.querySelector('[data-thread-anchor="1"]')?.textContent).toBe(
      "Turn interruptedtest interrupt detail"
    );
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it.each<[string, string, Observation]>([
    [
      "Turn lost test harness exited during the turn",
      "turn_completed",
      {
        case: "turnCompleted",
        value: { turnId: "test-turn", status: TurnStatus.PROCESS_LOST, error: "test harness exited during the turn" },
      },
    ],
    [
      "Turn ended without a status",
      "turn_completed",
      { case: "turnCompleted", value: { turnId: "test-turn", status: TurnStatus.UNSPECIFIED } },
    ],
    ["Harness lost", "harness_lost", { case: "harnessLost", value: {} }],
  ])("keeps an abnormal ending prominent: %s", async (text, observation, event) => {
    const container = await renderLifecycle(observation, event);
    expect(container.querySelector('[data-thread-anchor="1"] [role="alert"]')?.textContent).toBe(text);
  });
});
