// Result rendering for the in-process `gmail` server's `drafts_create` (the argument-side
// widgets live in ./requests.tsx). The tool returns the Gmail API Draft resource
// verbatim (gmail_api/messages.py's `Draft`, camelCase wire aliases); the draft `id` deep-links
// into Gmail's drafts view, where the operator reviews and sends it.

import { Anchor, Group } from "@mantine/core";
import { z } from "zod";

import { defineResultPreview, type ResultPreviewProps, type ToolResultPreview } from "../result_entry.tsx";
import { PreviewText } from "../vocabulary.tsx";

// Gmail's compose view opens a draft directly by its id.
function gmailDraftUrl(draftId: string): string {
  return `https://mail.google.com/mail/u/0/#drafts?compose=${draftId}`;
}

const zDraft = z.looseObject({ id: z.string() });
type Draft = z.infer<typeof zDraft>;

function CreateGmailDraftResultView({ result, variant }: ResultPreviewProps<Draft>) {
  // The link is the outcome; the raw draft id is provenance, so it rides along dimmed only in
  // detailed (the link's href carries it for anyone who needs it compact).
  return (
    <Group gap={6}>
      <Anchor href={gmailDraftUrl(result.id)} target="_blank" rel="noreferrer" size="sm">
        Open draft in Gmail ↗
      </Anchor>
      {variant === "detailed" && (
        <PreviewText span size="xs" c="dimmed">
          draft {result.id}
        </PreviewText>
      )}
    </Group>
  );
}

/** Per-tool result widgets for the `gmail` server. */
export const gmailResultPreviews = {
  drafts_create: defineResultPreview(zDraft, CreateGmailDraftResultView),
} satisfies Record<string, ToolResultPreview>;
