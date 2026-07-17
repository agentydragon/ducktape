# Spike: discovery-driven MCP tool generation for haku-google

Can we generate haku-console's Google MCP tool schemas from Google's API **Discovery Documents**
instead of hand-coding them? **Yes — for reads it's near-turnkey.** This spike proves it out.

Run: `python3 discovery_to_mcp.py` (pure stdlib; reads the vendored docs in `discovery_docs/`,
writes tools to `generated/`).

## What the spike does

`discovery_to_mcp.py` loads a discovery doc, finds a method by its dotted id (e.g.
`drive.files.list`), and converts its `parameters` (+ request-body `$ref` for writes) into an
MCP-style `{name, description, read_only, scopes, inputSchema}` — resolving `$ref`s against the
doc's `schemas`, mapping the Discovery dialect to JSON Schema, and guarding `$ref` cycles.

## Results (12 curated methods)

| tier  | tool                             | kind  | #props | inputSchema chars |
| ----- | -------------------------------- | ----- | ------ | ----------------- |
| P1    | `drive_files_list`               | read  | 15     | 4023              |
| P1    | `drive_files_get`                | read  | 6      | 1012              |
| P1    | `tasks_tasklists_list`           | read  | 2      | 310               |
| P1    | `tasks_tasks_list`               | read  | 12     | 2115              |
| P1    | `gmail_users_messages_list`      | read  | 6      | 1663              |
| P1    | `calendar_events_list`           | read  | 19     | 7846              |
| P2    | `drive_comments_list`            | read  | 5      | 892               |
| P2    | `drive_files_export`             | read  | 2      | 393               |
| P2    | `docs_documents_get`             | read  | 3      | 2032              |
| P2    | `sheets_spreadsheets_values_get` | read  | 5      | 3132              |
| WRITE | `calendar_events_insert`         | write | 8      | 35154             |
| WRITE | `gmail_users_drafts_create`      | write | 2      | 5976              |

**Reads convert cleanly** (300–8k chars): flat query params, typed, `enum`+`enumDescriptions`
folded into the description, `required` correct, arrays handled, and the per-method `scopes`
captured. Good enough to hand an LLM as-is (see `generated/tasks_tasks_list.json`,
`generated/gmail_users_messages_list.json`).

**Writes balloon** (`calendar.events.insert` → 44-property recursive `Event` body, 35k chars) —
they need hand-shaping. But writes are **not** on the read token and stay a separately-authored,
approval-gated surface, so this doesn't affect the read-tool goal.

## What auto-gen gives you vs. what stays hand-written

- **Generated:** `inputSchema`, param types/enums/descriptions, `required`, and the scope list
  (drives the scope-by-scope `$TOK` retirement). Execution is _also_ generic — the dynamic
  `googleapiclient` client runs any method by id, so no per-method call code.
- **Hand-written (the console's actual value):** per-Operator token resolution
  (`provider_connection.py`), the approval envelope, the read/write auto-approval policy, and a
  thin **curation overlay** per tool — e.g. drop `userId` (always `me`), hide noise params
  (`showDeleted`/`showAssigned`), tighten a description. ~80% of each tool is generated.

**Proposed shape:** a factory over a table of
`(api, version, method_id, tier, param_overrides, approval_policy)` → generated tool + generic
executor + console auth/policy. That's a concrete G3 implementation.

## Where the discovery docs come from (the versioning / typing question)

Three options; the docs are the single source for both MCP schemas **and** response typing (the
`schemas` map has `Message`/`Event`/`File`/`Task` etc., so one snapshot generates Pydantic/TS
types too — no drift between tool schema and types).

- **A. The pinned wheel's static cache.** `google-api-python-client==2.192.0` (already in
  `requirements_bazel.txt`) bundles every target API at
  `googleapiclient/discovery_cache/documents/*.json` — bazel-referenceable via the pypi dep, zero
  new infra. Caveat: the snapshot tracks the wheel release (currently `revision 20260112`, ~6
  months behind live `20260713`); bump the wheel to refresh. Fine for stable read params.
- **B. Vendor snapshots in-repo** (what `discovery_docs/` here demonstrates). Explicit, reviewable
  diffs when Google changes an API, decoupled from the wheel, deterministic codegen input. A small
  refresh script re-pulls from `https://www.googleapis.com/discovery/v1/apis/{api}/{v}/rest`.
- **C. Bazel-reference a versioned hosted source.** The **live** Discovery endpoint is
  latest-not-immutable → not reproducible, bad for `http_file`. But Google's
  `googleapis/discovery-artifact-manager` GitHub repo is the canonical **git-versioned** store of
  discovery artifacts → `http_archive`/git-pin a commit. This is the "hosted somewhere versioned"
  option — it exists.

**Recommendation:** B — vendor the ~7 snapshots we use and generate MCP schemas + response types
from them at build time, refreshed deliberately. A is fine if we'd rather not vendor; C if we want
pinning without committing JSON.

## Files

- `discovery_to_mcp.py` — the converter + curated allowlist.
- `discovery_docs/*.json` — vendored discovery snapshots (live `revision`, fetched for the spike).
- `generated/*.json` — generated MCP tools (regenerable; committed for review).
