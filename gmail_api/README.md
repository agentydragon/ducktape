# gmail_api

Shared Gmail API building blocks used across the repo's Gmail tooling
(`gmail_archiver/`, `haku/gmail_labeling/`).

- `labels.py` — Pydantic models and helpers for label resources: `GmailLabel`,
  `SystemLabel`, `resolve_label_id`, `is_system_label`, `CreateLabelRequest`, and
  the label visibility/type enums.
- `service.py` — `build_gmail_service(token_file)` builds an authenticated Gmail
  v1 client from an authorized-user OAuth token JSON (`gmail.modify` scope).
  `credentials_from_token_dir(token_dir, scopes)` is the generic (any Google API, any
  scopes) building block behind `build_gmail_service_from_token_dir` — reused by
  `haku/console/tools/google.py` to build both a Gmail and a Calendar service from one
  Airlock-rotated, multi-scope token.
