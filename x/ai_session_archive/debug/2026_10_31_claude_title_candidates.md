# Claude title candidates

During the native archive rebuild on 2026-10-31 UTC, the stored title projection
contained both `<local-command-caveat>` and `<fork-boilerplate>` prefixes.

The previous extractor accepted the first nonempty top-level `title`/`summary`
or user-message text, then stopped considering titles. Session updates also
filled only a missing title. Consequently both synthetic prompts and an early
prompt fallback could permanently win over later native metadata.

Read-only samples of captured payloads established these shapes:

- All 12 sampled local-command caveats were user records with `isMeta: true`.
- Command names and stdout were also user records, without `isMeta` in the
  samples, beginning with `<command-name>` and `<local-command-stdout>`.
- Fork records mixed `tool_result` and `text` blocks and were not marked meta.
  All eight sampled fork text blocks had a closed `<fork-boilerplate>` followed
  by `Your directive:` and the actual task.
- Native `custom-title` records had `customTitle` and `sessionId`; native
  `summary` records had `summary` and `leafUuid`.

These were samples of the partially rebuilt index, not an archive-wide census.
Conversation contents remain outside this note.

The title selector now keeps native custom names ahead of summaries and prompt
fallbacks. New file versions replace the projected Claude name. Existing file
versions retain their cached projection until explicitly refreshed; changing
title selection does not require a new raw-record parser version or another
database reset.
