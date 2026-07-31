// `drafts_create`'s pending and finished states are one evolving view, not two independent
// widgets: reuses CreateGmailDraftPreview (requests.tsx) verbatim while the call is pending —
// subject/recipients/body, everything the operator is being asked to approve — and
// CreateGmailDraftResultView (responses.tsx) once it has actually executed — a linked subject
// (Gmail's own icon marks it as external), still followed by the recipients/body from `args` (the
// operator is verifying what actually got drafted, so that content stays), plus the draft id in
// detailed. The card's error line (tool_call_card.tsx) already shows a failed call's message, so
// a failed/pending call keeps rendering the pending view — there's nothing to link to yet.
import { defineCallPreview, type ToolCallPreview } from "../call_entry.tsx";
import { CreateGmailDraftPreview, type CreateGmailDraftArgs, zCreateGmailDraftArgs } from "./requests.tsx";
import { CreateGmailDraftResultView, type Draft, zDraft } from "./responses.tsx";

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
export const gmailCallPreviews = {
  drafts_create: defineCallPreview(zCreateGmailDraftArgs, zDraft, CreateDraftCall),
} satisfies Record<string, ToolCallPreview>;
