## Action presentation conventions

- Actions side pane: each visible text adds distinct information. Caller's intent in the title, the
  custom Action label for operation and target, only additional arguments in the expanded preview;
  never repeat a label, namespace or selector the heading already states.
- A custom Action label replaces the technical `group / name` identity in the pane and details
  header. Use a readable phrase ("List pods in namespace tofu-controller"); fixture titles and
  accessible names must not reintroduce identifiers like `pods_list_in_namespace`.
- The whole collapsed action row (content and background) is the disclosure control; the separate
  right-arrow details link is the row's only navigation target.
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
- Offer card decision buttons when the compact card (label, collapsed and opened previews) shows
  the whole call; a destructive call is no exception.
