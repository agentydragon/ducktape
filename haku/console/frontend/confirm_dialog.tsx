import { Button, Group, Stack, Text } from "@mantine/core";
import type { PointerEvent } from "react";
import { useEffect, useRef, useState } from "react";

import type { GeolocationOptions } from "./bridge.ts";
import type { PendingApproval } from "./client.ts";
import { ACTION_COLOR } from "./theme.ts";

// The escalation the shell is asking the operator to approve — the **actual action** the
// iframe requested, as a disjoint union so each kind carries exactly its own fields and
// renders its own confirm copy (no flag+optional soup). The trusted-rendered text for each
// privileged action lives here, in one place, since this dialog is the only trustworthy
// surface at the moment of approval.
export type Escalation =
  | { kind: "openLink"; url: string }
  | { kind: "launch"; id: string; prompt: string }
  | { kind: "geolocation"; id: string; options?: GeolocationOptions }
  | { kind: "geolocationWatch"; id: string; options?: GeolocationOptions }
  | { kind: "toolApproval"; approval: PendingApproval };

interface Rendered {
  title: string;
  body?: string;
  // Verbatim agent-supplied text to review before approving (a URL, a launch prompt).
  preview?: { text: string; mono: boolean };
  approveLabel: string;
}

type ToolApprovalRenderer = (approval: PendingApproval) => Rendered;

const toolApprovalRenderers: Record<string, ToolApprovalRenderer> = {};

function defaultToolApprovalRenderer(approval: PendingApproval): Rendered {
  return {
    title: approval.title,
    body: `Approve ${approval.server_id}.${approval.tool_name} for ${approval.caller_principal}?`,
    preview: {
      text: JSON.stringify(
        {
          server_id: approval.server_id,
          tool_name: approval.tool_name,
          rationale: approval.rationale,
          arguments: approval.arguments,
          tool_call_id: approval.tool_call_id,
        },
        null,
        2
      ),
      mono: true,
    },
    approveLabel: "Run tool",
  };
}

function renderToolApproval(approval: PendingApproval): Rendered {
  const renderer = toolApprovalRenderers[`${approval.server_id}.${approval.tool_name}`] ?? defaultToolApprovalRenderer;
  return renderer(approval);
}

function render(action: Escalation): Rendered {
  switch (action.kind) {
    case "openLink":
      return { title: "Open this link?", preview: { text: action.url, mono: true }, approveLabel: "Open" };
    case "launch":
      return {
        title: "Launch a Haku run?",
        body: "This starts a new Claude Code web session running Haku now.",
        preview: action.prompt ? { text: action.prompt, mono: false } : undefined,
        approveLabel: "Launch",
      };
    // One grant covers both a one-shot read and a continuous watch, so the copy discloses
    // the strongest capability it unlocks — ongoing tracking.
    case "geolocation":
    case "geolocationWatch":
      return {
        title: "Allow Haku to use your location?",
        body: "Haku's UI is asking to use your device location, including tracking it continuously. Allowing lets it read your location whenever it asks — until you withdraw from the console panel (the ⚙ button, top-right). Your browser may prompt too. Haku is assumed adversarial; only allow when you trust why it's asked.",
        approveLabel: "Allow",
      };
    case "toolApproval":
      return renderToolApproval(action.approval);
  }
}

// Top-layer modal confirm — the only trustworthy surface at the moment of a privileged
// action. Rendered with the native `<dialog>.showModal()` (the browser **top layer**)
// so the cross-origin iframe cannot draw over it, read it, or intercept its clicks.
// Mantine's Modal/Portal and CSS z-index can mimic the layout, but they cannot move an
// arbitrary element into the browser top layer. The `::backdrop` dims the agent UI so
// "the shell is talking now" is unambiguous. The approve button stays disabled for a
// beat so a baited click-through can't land on a freshly-rendered confirm. See
// docs/containment.md → "The shell as a thin trusted layer".
const ARM_DELAY_MS = 400;

function pointerDownOutsideDialog(e: PointerEvent<HTMLDialogElement>): boolean {
  const rect = e.currentTarget.getBoundingClientRect();
  return e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom;
}

export function ConfirmDialog({
  action,
  onApprove,
  onCancel,
}: {
  action: Escalation | null;
  onApprove: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (action && !d.open) {
      setArmed(false);
      d.showModal();
      const t = setTimeout(() => setArmed(true), ARM_DELAY_MS);
      return () => clearTimeout(t);
    }
    if (!action && d.open) d.close();
  }, [action]);

  const r = action ? render(action) : null;
  return (
    <dialog
      ref={ref}
      onPointerDown={(e) => {
        if (pointerDownOutsideDialog(e)) onCancel();
      }}
      onCancel={(e) => {
        e.preventDefault(); // route Esc through our handler so the iframe gets a result
        onCancel();
      }}
      className="haku-confirm-dialog max-w-md rounded-lg p-5 shadow-xl"
    >
      {r && (
        <Stack gap="sm">
          <Text fw={600}>{r.title}</Text>
          {r.body && <Text size="sm">{r.body}</Text>}
          {r.preview && (
            <Text
              size="sm"
              className={`haku-url-preview rounded p-2 ${r.preview.mono ? "font-mono break-all" : "break-words whitespace-pre-wrap"}`}
            >
              {r.preview.text}
            </Text>
          )}
          <Group justify="flex-end" gap="sm" mt="xs">
            <Button variant="default" size="xs" onClick={onCancel}>
              Cancel
            </Button>
            <Button color={ACTION_COLOR} size="xs" disabled={!armed} onClick={onApprove}>
              {r.approveLabel}
            </Button>
          </Group>
        </Stack>
      )}
    </dialog>
  );
}
