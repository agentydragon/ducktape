# gmail_api

Shared Gmail API building blocks used across the repo's Gmail tooling
(`gmail_archiver/`, `haku/console/tools/`).

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
  `credentials_from_token_dir(token_dir, scopes)` builds google-auth credentials whose
  `refresh_handler` re-reads a mounted access-token dir (the Airlock-mounted-token path); it and
  `build_gmail_service_from_token_dir` remain for that path. (haku-console's per-Operator path
  builds bearer-only `Credentials(token=…)` directly from its own kept-fresh token.)
