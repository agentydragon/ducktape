# Schema/codegen design (#12342) and the four open PRs parked on it

2026-11-10. Read-only review of five open PRs against `origin/devel` at `7f4830b01f`. Every PR is 3,000–6,000 commits
behind `devel`. Delete this note once the operator has disposed of them.

## Recommendation

Close all five. `devel` has already adopted each design choice they propose or has replaced it. #12273 is
the exception: its goal, runtime validation of first-party HTTP responses in AIQuota and Spend, never landed. Whether
that goal is wanted is the one open question (see the end of this note).

## #12342: Document current schema pipelines with cross-language spikes

**What it decides.** It adds a 515-line `devinfra/docs/schema_codegen.md` inventory and 76 spike files under
`devinfra/x/schema_codegen/`. The rules it sets:

- Converters read declared schema files and never run a backend exporter; remote `$ref`s are never fetched.
- OpenAPI, standalone JSON Schema, protobuf, Swagger 2, Discovery and CRD schemas each keep their own converter. Nothing
  converts every format through one universal intermediate.
- Generated TypeScript exports named model types (`rootTypes`), and the normal TypeScript rules compile the output.
- ProtoJSON uses protobuf codecs (int64, Timestamp, oneof, presence and future enum values).
- Pydantic and Go definitions are the sources of truth.

**Already landed on `devel`, following it.**

- `b1424ce072` (#12914) replaced the inventory with a 33-line boundary map at `devinfra/docs/schema_codegen.md`. Its
  commit message recommends closing #12342. `342a3e0e0f` (#14577) trimmed the map further.
- The STYLE.md TypeScript section ("Generated model types") and `devinfra/js/SCHEMA_CODEGEN.md` carry the named-export
  and compile-with-normal-rules rules.
- `b710f89c60` (#12484) landed the file-input `json_schema_js_library` and removed the legacy OpenAPI-to-Zod helper.
- `//devinfra/js:schema_remote_fetch_guard` wraps all three converters.
- `a96bfb2171` (#14856) moved ProtoJSON decoding to Buf's `fromJson`/`fromJsonString`.

**Already landed on `devel`, contradicting it or showing it is stale.**

- `46e4e6a0fe` (#14149) makes protobuf the canonical form of History Thread entities. Pydantic and Go are no longer the
  only sources of truth.
- The PR's inventory still names `agentplane/app/export_schema.py` (deleted; `agentplane/app/go/schema_export.go`
  exports through Huma) and `agentplane/runner/codex/protocol` (moved to `agentplane/env/runtime/codex/protocol`).
- Both the PR and `devel` add `devinfra/docs/schema_codegen.md`, so the PR conflicts.

**Recommendation: close.** #12914 already moved its findings to `devinfra/docs/`. One spike is still worth keeping:
nothing on `devel` tests that the remote-fetch guard fails closed. Port
`devinfra/x/schema_codegen/typescript/schema_remote_references.test.mjs` (on the PR branch) into a test beside
`devinfra/js/schema_remote_fetch_guard.mjs` as a separate small PR. The other spike cases each pin a pipeline that
production code already exercises (#12914 checked them case by case).

## The four gated PRs

**#11570, TS proto closure with Protobuf-ES.** The triage did not call this one overtaken. My assessment is that
`devel` chose a different design. `9d9d27c77c` (#13780) registered the shared Protobuf-ES toolchain
(`//devinfra/js/proto`) and `stage_protobuf_es` for external protos. Per-root `ts_proto_library` stays in about 14
BUILD files, and `rules_proto_grpc` is no longer in `MODULE.bazel`. The only part not done is retiring the copied
`//google/api` package, which `notifications_pb` and History's TS bindings still use.
**Close.** If wanted, open a small follow-up that moves `//google/api` to `stage_protobuf_es`.

**#12273, Zod validators for live API responses.** The claim is only partly right. `b710f89c60` deleted
`devinfra/js/openapi.bzl`, the macro this PR edits. That overtakes the mechanism, not the goal. Today
`openapi_zod_ts_library` serves only Augur and Study Casino, and neither `aiquota/frontend` nor
`finance/plaid/spend/ui` validates responses. The Agentplane third is overtaken: sandbox live views now decode
protobuf watches (`agentplane/app/frontend/sandboxes/live.tsx`). The `devel` map limits runtime Zod to "untrusted or
versioned peers", which argues against this PR. **Close**, unless the operator wants that validation (question
below). Then rewrite it as one PR per app on `openapi_zod_ts_library`, starting from its leaves #12275 (AIQuota)
and #12286 (Spend). GitHub shows those leaves as merged, but they merged into this PR's branch
`codex/openapi-zod-config`; none of their merge commits are on `devel`.

**#12303, decode snapshots before marking streams live.** Mostly overtaken. `358d3919ba` (#14096) replaced the
notification snapshot stream it hardens with Session inbox watches, which the frontend decodes with generated messages
(`211ed4128f` #13461) inside a Zod-checked envelope (`a96bfb2171` #14856). Its base branch belongs to a closed PR, so it
cannot land as stacked. One behavior did not land: `followResponse` in `agentplane/app/frontend/stream_reader.ts`
reports `live` before `onFrame` runs, so a malformed first frame shows `live` briefly and then reconnects. **Close.** If
wanted, fix the ordering as a separate small PR; no schema decision is involved.

**#12304, dispatcher watch decoder.** Mostly overtaken. `c6b7c38d17` (#12867) decodes watch frames through the
generated Dispatcher ProtoJSON schema (`dispatchFromJson`), and `a96bfb2171` validates the envelope. It is also stacked
on a closed PR's branch. One behavior differs: `threads/dispatch_watch.ts` logs an unapplicable frame and continues,
where the PR treated it as a read error and kept future numeric delivery states nonterminal. **Close.** Revisit the
unknown-state handling only if a new Dispatcher state ships.

## Operator question

Should first-party HTTP frontends (AIQuota, Spend) validate API responses at runtime with generated Zod, or does the
`devel` rule "runtime Zod only for untrusted or versioned peers" stand?

- If the rule stands, close all five.
- If runtime validation is wanted, close four and rewrite #12273 as one PR per app.

The optional follow-ups (guard test, `//google/api` staging, live-after-frame ordering) do not depend on the answer.
