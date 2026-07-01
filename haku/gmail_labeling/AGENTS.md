@README.md

The closure invariant (`SPEC.md`) is enforced in `client.py`, before any Gmail
call — never in the agent prompt. When adding a tool, route every label-name
argument through `LabelNamespace.require` (or a method that does) so a new verb
cannot widen the namespace. `rename_label` must validate both names;
`modify_labels` must validate every name in both `add` and `remove` before
touching the backend.
