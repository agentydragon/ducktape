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
- Match the detail on the decision surface to the risk of the Action. Show the operation, target
  and account scope, material effects, and any exposure. Require fuller review when those depend
  on additional arguments or resolved data. Keep the exact arguments available in full review,
  and send unknown or unsupported arguments there for a decision.
- Low-risk details can stay under disclosure. An unsent Gmail draft can be decided on the
  collapsed card when its subject, recipients, and any reply thread are visible. The full body
  remains available on expansion.
- For a read with inline decisions, show the source, account or target scope, and options that
  materially change access or exposure. A Calendar event list can leave its date window under
  disclosure when the card makes the broader read clear. Give changes to scope or side effects
  the scrutiny their consequences warrant, even when the primary operation is a read.
