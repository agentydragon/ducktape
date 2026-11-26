# The four open PRs parked on the schema/codegen design (#12342)

2026-11-10. Read-only review of #11570, #12273, #12303 and #12304 against `origin/devel` at `7f4830b01f`. Each was
held for #12342, which is now closed; `devel`'s design record is `devinfra/docs/schema_codegen.md` (`b1424ce072`
#12914). Every PR is 3,000–6,000 commits behind `devel`. Delete this note once the operator has disposed of them.

## Recommendation

Close all four. `devel` has already adopted each design choice they propose or has replaced it. #12273 is the
exception: its goal, runtime validation of first-party HTTP responses in AIQuota and Spend, never landed. Whether that
goal is wanted is the one open question (see the end of this note).

## The four PRs

**#11570, TS proto closure with Protobuf-ES.** The triage did not call this one overtaken. My assessment is that
`devel` chose a different design. `9d9d27c77c` (#13780) registered the shared Protobuf-ES toolchain
(`//devinfra/js/proto`) and `stage_protobuf_es` for external protos. Per-root `ts_proto_library` stays in about 14
BUILD files, and `rules_proto_grpc` is no longer in `MODULE.bazel`. The only part not done is retiring the copied
`//google/api` package, which `notifications_pb` and History's TS bindings still use.
**Close.** If wanted, open a small follow-up that moves `//google/api` to `stage_protobuf_es`.

**#12273, Zod validators for live API responses.** The triage's "overtaken" claim is only partly right. `b710f89c60`
deleted `devinfra/js/openapi.bzl`, the macro this PR edits. That overtakes the mechanism, not the goal. Today
`openapi_zod_ts_library` serves only Augur and Study Casino, and neither `aiquota/frontend` nor `finance/plaid/spend/ui`
validates responses. The Agentplane third is overtaken: sandbox live views now decode protobuf watches
(`agentplane/app/frontend/sandboxes/live.tsx`). The `devel` map limits runtime Zod to "untrusted or versioned peers",
which argues against this PR. **Close**, unless the operator wants that validation (question below). Then rewrite it as
one PR per app on `openapi_zod_ts_library`, starting from its leaves #12275 (AIQuota) and #12286 (Spend). GitHub shows
those leaves as merged, but they merged into this PR's branch `codex/openapi-zod-config`; none of their merge commits
are on `devel`.

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

- If the rule stands, close all four.
- If runtime validation is wanted, close three and rewrite #12273 as one PR per app.

The optional follow-ups do not depend on the answer:

- Move `//google/api` to `stage_protobuf_es` (#11570's only unfinished part).
- Report a stream as `live` only after its first frame is accepted (#12303).
- Port `devinfra/x/schema_codegen/typescript/schema_remote_references.test.mjs` (#12342 head `b718fd4543`) into a test
  beside `devinfra/js/schema_remote_fetch_guard.mjs`; nothing on `devel` tests that the guard fails closed.
