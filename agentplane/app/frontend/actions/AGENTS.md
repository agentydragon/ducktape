## Action presentation conventions

- Actions side pane: each visible text adds distinct information. Caller's intent in the title, the
  custom Action label for operation and target, only additional arguments in the expanded preview;
  never repeat a label, namespace or selector the heading already states.
- A custom Action label replaces the technical `group / name` identity in the pane and details
  header. Use a readable phrase ("List pods in namespace tofu-controller"); fixture titles and
  accessible names must not reintroduce identifiers like `pods_list_in_namespace`.
- The whole collapsed action row (content and background) is the disclosure control; the separate
  right-arrow details link is the row's only navigation target.
- Never render a lone Approve or Deny: use the shared `ActionDecisionButtons` (green/check Approve,
  red/cross Deny).
