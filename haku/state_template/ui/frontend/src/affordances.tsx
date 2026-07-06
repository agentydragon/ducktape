import { Anchor, Button, Group, Stack, Text, Textarea } from "@mantine/core";
import { Children, createContext, useContext, useEffect, useState, type ReactNode } from "react";

import { openLink, requestLaunch, type LaunchResult } from "./bridge.ts";
import { callToolRequest, clearResponse, readResponse, sendFeedback, setResponse } from "./client.ts";
import { notifyError } from "./errors.ts";
import { ItemScopeContext } from "./item_scope.ts";
import { logger } from "./log.ts";
import type { ToolCallRecord } from "./types.ts";

const log = logger("affordances");

// Affordance widgets — a growing library of reviewed, embeddable action buttons Haku can drop
// free-form into any item/note body (via the garden renderer's registry), instead of a rigid
// typed item-action model. Each wraps an ALREADY-gated capability, so the registry grows while
// the trust boundary is untouched (openLink is scheme+whitelist-gated by the console; feedback /
// launch are bounded endpoints). See plans/garden-gradient.md → What does NOT move #2.

// Claude logomark (simple-icons, CC0) — inlined as an SVG so it needs no external asset (the
// gateway CSP blocks remote images); `currentColor` so it takes the button's text color.
function ClaudeMark() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style={{ display: "block" }}>
      <path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z" />
    </svg>
  );
}

// "Hand off to Claude": open a fresh claude.ai conversation seeded with `prompt`, via the shell's
// gated openLink (claude.ai is whitelisted → opens directly). `label` should be a very short
// imperative summary of what the handoff will do — "Debug test_foo flakiness", "Draft the York
// reply" — not the full prompt (which is what actually seeds the conversation). Replaces the old
// prepared_prompt / claude_handoff item-action kinds.
export function Handoff({ prompt, label = "Send to Claude" }: { prompt: string; label?: string }) {
  return (
    <Button
      size="xs"
      variant="default"
      leftSection={<ClaudeMark />}
      onClick={() => void openLink(`https://claude.ai/new?q=${encodeURIComponent(prompt)}`)}
    >
      {label}
    </Button>
  );
}

// "Launch run": ask the shell to start a Haku run seeded with `prompt`. The privileged launch
// capability is fired by the shell after its OWN trusted confirm (the iframe can only ask —
// bridge.ts:requestLaunch), so this button is safe to embed anywhere. Replaces the old `command`
// item-action's launch semantics. On success we surface the session link (opened via the gated
// openLink); a cancel/failure shows the shell's reason.
export function Launch({ prompt, label = "Launch run" }: { prompt: string; label?: string }) {
  const [state, setState] = useState<"idle" | "pending" | LaunchResult>("idle");
  if (state !== "idle" && state !== "pending") {
    return state.ok ? (
      <Group gap="xs">
        <Text size="xs" c="teal">
          ✓ launched
        </Text>
        {state.sessionUrl && (
          <Anchor size="xs" style={{ cursor: "pointer" }} onClick={() => void openLink(state.sessionUrl!)}>
            open session →
          </Anchor>
        )}
      </Group>
    ) : (
      <Group gap="xs">
        <Button size="xs" variant="default" onClick={() => setState("idle")}>
          {label} →
        </Button>
        <Text size="xs" c="dimmed">
          {state.reason ?? "cancelled"}
        </Text>
      </Group>
    );
  }
  return (
    <Button
      size="xs"
      variant="default"
      loading={state === "pending"}
      onClick={() => {
        setState("pending");
        void requestLaunch(prompt).then(setState);
      }}
    >
      {label} →
    </Button>
  );
}

function toolCallMessage(record: ToolCallRecord): { color: string; text: string } {
  if (record.status === "ok") return { color: "teal", text: "✓ ran" };
  if (record.status === "approval_required") return { color: "dimmed", text: "waiting in console" };
  if (record.status === "running") return { color: "dimmed", text: "running" };
  if (record.status === "denied") return { color: "red", text: record.decision_reason ?? "denied" };
  if (record.status === "timed_out") return { color: "red", text: "timed out" };
  if (record.status === "not_allowed") return { color: "red", text: record.error ?? "not allowed" };
  return { color: "red", text: record.error ?? "failed" };
}

export function ToolCall({ request, label = "Run tool" }: { request?: string; label?: string }) {
  const [state, setState] = useState<"idle" | "pending" | ToolCallRecord>("idle");
  const requestId = request?.trim();

  if (!requestId) {
    return (
      <Text size="xs" c="red">
        &lt;tool-call&gt; needs a request
      </Text>
    );
  }

  const record = typeof state === "object" ? state : null;
  const message = record ? toolCallMessage(record) : null;
  const buttonLabel =
    record && record.status !== "ok" && record.status !== "approval_required" && record.status !== "running"
      ? `${label} again`
      : label;

  return (
    <Group gap="xs">
      <Button
        size="xs"
        variant="default"
        loading={state === "pending"}
        disabled={record?.status === "ok"}
        onClick={() => {
          setState("pending");
          void callToolRequest(requestId).then(setState, (e: unknown) => {
            notifyError("Couldn't request tool call", e);
            setState("idle");
          });
        }}
      >
        {buttonLabel}
      </Button>
      {message && (
        <Text size="xs" c={message.color}>
          {message.text}
        </Text>
      )}
    </Group>
  );
}

// Quick-feedback button: one click appends `text` to the operator's feedback trace (client.ts:
// sendFeedback, a bounded POST). For canned reactions Haku can offer inline — `<feedback
// text="not useful" label="👎 not useful"></feedback>` — instead of a free-form box. Optional
// `item` scopes the feedback to an item id.
export function Feedback({ text, label = "Send feedback", item }: { text: string; label?: string; item?: string }) {
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  if (state === "sent")
    return (
      <Text size="xs" c="teal">
        ✓ sent
      </Text>
    );
  return (
    <Button
      size="xs"
      variant="default"
      loading={state === "sending"}
      onClick={() => {
        setState("sending");
        void sendFeedback(text, item).then(
          () => setState("sent"),
          (e: unknown) => {
            notifyError("Couldn't send feedback", e);
            setState("idle");
          }
        );
      }}
    >
      {label}
    </Button>
  );
}

// Outcome capture: a single-select "slot" — a question (`prompt`) answered by picking one of its
// `<choice>` children, plus an always-present "Other…" escape that opens a free-text box. Composes
// like <select>/<option> (a <choices> owns the slot, each <choice> is one answer), the radio-button
// shape the operator asked for: a slot holds at most one answer. It records the answer as a feedback
// note (client.sendFeedback — already gated), so Haku reads it on its next run and plans the
// follow-up; the single-select is the UX contract, the intake note is the wire. Generalizes both a
// one-option "done" acknowledgement and richer appointment-outcome captures; a kitchen-signal
// variant wired to setKitchenSignal (true replace-in-slot semantics) is the natural next step.
//
//   <choices prompt="How did the dentist visit go?" item="…">
//   <choice value="Missed it"></choice>
//   <choice value="went">Went, as expected</choice>
//   </choices>

// A <choices>/<signal-toggle> owns the slot state; each <choice> child reports its pick up through
// this context, so a <choice> stays a pure button with no prop-threading through the markdown
// renderer. `pending` is the value currently being sent (spinner). `selected` is present only for a
// stateful parent (<signal-toggle>): it marks the current answer so the button renders pressed;
// `undefined` (report-once <choices>) means no button is ever "active".
interface ChoicesCtx {
  pick: (value: string) => void;
  pending: string | null;
  selected?: string | null;
}
const ChoicesContext = createContext<ChoicesCtx | null>(null);

export function Choices({ item, prompt, children }: { item?: string; prompt?: string; children?: ReactNode }) {
  const [recorded, setRecorded] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null); // value of the answer currently sending
  const [otherOpen, setOtherOpen] = useState(false);
  const [otherText, setOtherText] = useState("");

  // Record `answer` under this slot: the feedback note carries the question so Haku knows what was
  // answered, then the answer; `recordedLabel` is what we echo back to the operator.
  function submit(key: string, recordedLabel: string, answer: string) {
    setPending(key);
    void sendFeedback(prompt ? `${prompt} → ${answer}` : answer, item).then(
      () => {
        setRecorded(recordedLabel);
        setPending(null);
        setOtherOpen(false);
      },
      (e: unknown) => {
        notifyError("Couldn't record your answer", e);
        setPending(null);
      }
    );
  }

  if (recorded !== null && !otherOpen)
    return (
      <Group gap="xs">
        <Text size="xs" c="teal">
          ✓ recorded: {recorded}
        </Text>
        <Anchor size="xs" style={{ cursor: "pointer" }} onClick={() => setRecorded(null)}>
          change
        </Anchor>
      </Group>
    );

  return (
    <Stack gap={6}>
      {prompt && (
        <Text size="sm" fw={500}>
          {prompt}
        </Text>
      )}
      <Group gap="xs">
        <ChoicesContext.Provider value={{ pick: (v) => submit(v, v, v), pending }}>{children}</ChoicesContext.Provider>
        <Button size="xs" variant="subtle" onClick={() => setOtherOpen((v) => !v)}>
          Other…
        </Button>
      </Group>
      {otherOpen && (
        <Group gap="xs" align="flex-end">
          <Textarea
            rows={2}
            placeholder="Describe how it went…"
            value={otherText}
            onChange={(e) => setOtherText(e.currentTarget.value)}
            style={{ flex: 1 }}
          />
          <Button
            size="xs"
            disabled={!otherText.trim()}
            loading={pending === "__other__"}
            onClick={() => submit("__other__", `other — ${otherText.trim()}`, `other: ${otherText.trim()}`)}
          >
            Send
          </Button>
        </Group>
      )}
    </Stack>
  );
}

// One answer inside a <choices> slot. `value` is what gets recorded; optional children override the
// visible label, mirroring `<option value="x">Label</option>`. Outside a <choices> it renders
// nothing — a <choice> only means something within a slot.
export function Choice({ value = "", children }: { value?: string; children?: ReactNode }) {
  const ctx = useContext(ChoicesContext);
  if (!ctx) return null;
  const tracks = ctx.selected !== undefined; // stateful parent (<signal-toggle>) vs report-once
  const active = tracks && ctx.selected === value;
  return (
    <Button
      size="xs"
      variant={active ? "filled" : "outline"}
      color={active ? "teal" : undefined}
      aria-pressed={tracks ? active : undefined}
      loading={ctx.pending === value}
      onClick={() => ctx.pick(value)}
    >
      {Children.count(children) > 0 ? children : value}
    </Button>
  );
}

// Stateful sibling of <choices>: a single-select slot backed by the responses log (client.setResponse
// / clearResponse), keyed by (scope, field). Unlike <choices> (report-once → intake note), it
// **prefills** the current answer (readResponse) and shows it pressed, and re-picking the active
// answer clears the slot — radio-with-retract, the shape kitchen signals and an item status slot
// want. Authored the same way, with <choice> children:
//   <signal-toggle scope="dentist-appt" field="status" prompt="Booked?">
//   <choice value="yes">Yes</choice>
//   <choice value="no">No</choice>
//   </signal-toggle>
export function SignalToggle({
  scope,
  field,
  prompt,
  children,
}: {
  scope?: string;
  field: string;
  prompt?: string;
  children?: ReactNode;
}) {
  // `scope` may come from the tag, or (inside an item body) from the enclosing <item-card>.
  const itemScope = useContext(ItemScopeContext);
  const resolvedScope = scope || itemScope;
  const [selected, setSelected] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  useEffect(() => {
    if (!resolvedScope) return;
    let live = true;
    void readResponse(resolvedScope, field).then(
      (v) => {
        if (live) setSelected(v);
      },
      (e: unknown) => {
        // Prefill is best-effort — a read failure just leaves the slot unanswered — but log it.
        log.warn(`signal-toggle prefill read failed for ${resolvedScope}/${field}`, e);
      }
    );
    return () => {
      live = false;
    };
  }, [resolvedScope, field]);

  if (!resolvedScope)
    return (
      <Text size="xs" c="red">
        &lt;signal-toggle&gt; needs a scope (an attribute, or an enclosing item)
      </Text>
    );
  const activeScope = resolvedScope; // narrowed to string; re-bound so the pick closure keeps it

  function pick(value: string) {
    const clearing = selected === value; // re-picking the active answer retracts it
    setPending(value);
    const op = clearing ? clearResponse(activeScope, field) : setResponse(activeScope, field, value);
    void op.then(
      () => {
        setSelected(clearing ? null : value);
        setPending(null);
      },
      (e: unknown) => {
        notifyError("Couldn't save your answer", e);
        setPending(null);
      }
    );
  }

  return (
    <Stack gap={6}>
      {prompt && (
        <Text size="sm" fw={500}>
          {prompt}
        </Text>
      )}
      <Group gap="xs">
        <ChoicesContext.Provider value={{ pick, pending, selected }}>{children}</ChoicesContext.Provider>
      </Group>
    </Stack>
  );
}
