# gmail_api

Shared Gmail API building blocks used across the repo's Gmail tooling
(`gmail_archiver/`, `haku/gmail_labeling/`, `haku/console/tools/`).

- `labels.py` — Pydantic models and helpers for label resources: `GmailLabel`,
  `SystemLabel`, `resolve_label_id`, `is_system_label`, `CreateLabelRequest`,
  `PatchLabelRequest`, `LabelsListResponse`, and the label visibility/type enums.
- `messages.py` — Pydantic models mirroring Gmail's message/thread/draft resources
  (`Message`, `MessagePart`, `MessagePartBody`, `MessagePartHeader`, `Thread`, `Draft`,
  `ThreadsListResponse`) plus the `MessageFormat`/`ThreadFormat` enums. Camel-cased on the
  wire (`to_camel` aliases + `populate_by_name`), so an API JSON response validates directly
  with `Model.model_validate(response)` and serializes back unchanged.
- `service.py` — `build_gmail_service(token_file)` builds an authenticated Gmail
  v1 client from an authorized-user OAuth token JSON (`gmail.modify` scope).
  `credentials_from_token_dir(token_dir, scopes)` is the generic (any Google API, any
  scopes) building block behind `build_gmail_service_from_token_dir` — reused by
  `haku/console/tools/{gmail,google}.py` to build the Gmail and Calendar services from one
  Airlock-rotated, multi-scope token.
