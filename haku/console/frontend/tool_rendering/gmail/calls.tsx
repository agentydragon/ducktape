// `drafts_create`'s pending and finished states are one evolving view: CreateGmailDraftPreview
// (requests.tsx) while the call is pending, CreateGmailDraftResultView (responses.tsx) once it has
// executed. A failed call keeps rendering the pending view — there is nothing to link to yet, and
// the card's error line (tool_call_card.tsx) already shows the message.
import { defineCallPreview, type ToolCallPreview } from "../call_entry";
import { CreateGmailDraftPreview, type CreateGmailDraftArgs, zCreateGmailDraftArgs } from "./requests";
import { CreateGmailDraftResultView, type Draft, zDraft } from "./responses";

function CreateDraftCall({
  args,
  result,
  variant,
}: {
  args: CreateGmailDraftArgs;
  result: Draft | undefined;
  variant: "compact" | "detailed";
}) {
  if (result) return <CreateGmailDraftResultView args={args} result={result} variant={variant} />;
  return <CreateGmailDraftPreview args={args} variant={variant} />;
}

/** Combined pending/finished widgets for the `gmail` server. */
export const gmailCallPreviews: {
  drafts_create: ToolCallPreview<typeof zCreateGmailDraftArgs, typeof zDraft>;
} = {
  drafts_create: defineCallPreview(zCreateGmailDraftArgs, zDraft, CreateDraftCall),
} satisfies Record<string, ToolCallPreview>;
