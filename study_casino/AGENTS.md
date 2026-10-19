@README.md

## In-app changelog

A user-visible behavior change (economy rules, game mechanics, balance migrations; not cosmetic
or refactor) appends a `ChangelogEntry` to <changelog.py> with the next sequential `id`, ship
date and brief description. Users see unacked entries on their next visit.

## Typed wire surface

Pydantic models in <state.py> (reads), <actions.py> (action results) and <events.py> are the
source of truth for FastAPI routes and the Zod schemas `//study_casino/frontend/lib:schema_zod`
generates (via <export_schema.py>; any `bbr build //study_casino/...` regenerates them). A
renamed or removed field surfaces as a `ZodError` at the frontend parse site.

New endpoint:

1. `response_model=...` or a typed return, never `dict[str, Any]`.
2. Mutations return a typed `ActionResponse`; a genuinely new shape gets a new `ActionResult`
   variant with `extra="forbid"` and a unique required-field set (the union is structural; a
   `kind` discriminator is being rolled out per <docs/action_result_discriminators.md>).
3. A `game_events` row is returned as `ActionMutation(game_event=GameEventMutation(...))` with a
   `RouletteOutcome` / `SlotsOutcome` / `BlackjackOutcome` (<events.py>).
4. Parse the response in `frontend/sync.js` (or `use_casino.js`) through its `z<ResponseModel>`.
