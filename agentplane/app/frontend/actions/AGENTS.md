@README.md

## Action presentation conventions

- Actions side pane: each visible text adds distinct information. Caller's intent in the title, the
  custom Action label for operation and target, only additional arguments in the expanded preview;
  never repeat a label, namespace or selector the heading already states.
- A custom Action label replaces the technical `group / name` identity in the pane and details
  header. Use a readable phrase ("List pods in namespace tofu-controller"); fixture titles and
  accessible names must not reintroduce identifiers like `pods_list_in_namespace`.
- Use the collapsed row's content and background for disclosure, with a separate right-arrow link
  for full review. A resource link in the row remains independently focusable and clickable.
- A card without inline Approve and Deny can give a short target and intent summary. Put the
  information needed to decide in the fuller review, including the opened preview when it offers
  decision buttons.
- No disclosure when opening would add nothing: an Action whose closed compact card already shows
  every argument declares `compactCardExpands` false for that invocation
  (`expandsForRejectedArguments`), and drops its `pane.opened` slot.
- A command is always highlighted shell: `ShellCommandSummary` on the compact card's collapsed row, cut
  off after three lines; `ShellCommandView` in the opened preview and the full card, which hold all
  of it. Never a bare `<Code>` of a command.
- Never render a lone Approve or Deny: use the shared `ActionDecisionButtons` (green/check Approve,
  red/cross Deny).
- Command-running Actions (SSH, Pod and sandbox exec) read alike: the label is "Run command on/in
  <target>", the pane never repeats the target, and the settings that change how it runs are one
  line of `ShellCommandView` notes built by a `schemas/` helper that the notification text shares.
- For mutations, show the target, scope, and consequential effects before offering card decision
  buttons. If review needs the opened preview or live resolution of a target, gate the buttons on
  that preview and its resolved data. Creating an unsent Gmail draft stores a message for later
  review, so the collapsed card can offer a decision with the recipients, subject, and any reply
  thread visible. Keep the full message body available on expansion.
- A compact card for a read may leave narrowing constraints in the opened or full view when the
  broader read is clear from the card. Show the data source and account or target scope, and any
  option that can broaden the read, change what it returns, or cause side effects. Unknown or
  unsupported arguments require full review.
