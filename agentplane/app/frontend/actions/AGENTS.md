## Action presentation conventions

- In the Actions side pane, each piece of visible text should add distinct information. Keep the
  caller's intent in the title, let a custom Action label describe the operation and target, and
  show only additional arguments in the expanded preview. Do not repeat a label, namespace, or
  selector in multiple places when the heading already says it.
- A custom Action label replaces the technical `group / name` identity in the pane and details
  header. Prefer a readable phrase such as “List pods in namespace tofu-controller”; fixture titles
  and accessible names should not reintroduce identifiers like `pods_list_in_namespace`.
- The collapsed action row is a disclosure control across its full content and background. Keep the
  separate right-arrow details link as the only navigation target in that row; do not require a click
  on the chevron alone.
- Never render a lone Approve or Deny decision. Use the shared `ActionDecisionButtons` widget so both
  choices stay together, with green/check for Approve and red/cross for Deny.
