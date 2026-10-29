# gmail_api

Shared Gmail API building blocks used across the repo's Gmail tooling
(`gmail_archiver/`, `x/google_mcp_server/`).

- `labels.py` — Pydantic models and helpers for label resources: `GmailLabel`,
  `SystemLabel`, `resolve_label_id`, `is_system_label`, `CreateLabelRequest`,
  `PatchLabelRequest`, and the label visibility/type enums.
- `messages.py` — Pydantic models mirroring Gmail's message/draft resources
  (`Message`, `MessagePart`, `MessagePartBody`, `MessagePartHeader`, `Draft`). Camel-cased on the
  wire (`to_camel` aliases + `populate_by_name`), so an API JSON response validates directly
  with `Model.model_validate(response)` and serializes back unchanged.
- `service.py` — `build_gmail_service(token_file)` builds the Gmail v1 client used by
  `gmail_archiver/` from a standard google-auth authorized-user OAuth token JSON. The
  `x/google_mcp_server/` backend builds bearer-only credentials directly from its Airlock token.
