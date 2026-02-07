# Compress Implementation to Prompt

Compress a codebase (file/dir/project) into an implementation-agnostic prompt that an advanced LLM
can use to reimplement an equivalent system. Emphasize externally observable behavior and testable
contracts; avoid leaking internal names/schemas unless strictly necessary.

## Input

- Infer from context or user input: input artifact of any granularity — single function, one file
  (`app/models.py`, an integration test, small CLI), full apps (Django, GUI, MCP server, ...),
  IPython notebook with an ML experiment, ...
- Intelligently infer from context any exclusions (e.g., experimental/legacy/generated code) and constraints.
- User may additionally clarify scope (code to compress), spec highlights/inclusions/omissions or priorities.
- Ask clarifying questions when critical ambiguities exist. Examples:
  - "Is wire logging be a hard requirement, or should we just list observability as a high-level feature?"
  - "UI is implemented as responsive, layouts for mobile, tablet and desktop. Should that be a a requirement?"
  - "Should spec explicitly set Postgres as the required DB, or is any DB fine as long as requirements are met?"
  - "Exclusions: spec requirement for <200 ms latency implies caching is needed. Shall we accept any implementation that meets the <200 ms requirement (and omit explicit caching requirements), or should we explicitly list caching as required?"

## Output

Markdown file with the compressed spec/prompt, suitable for handoff to a reimplementation LLM agent.

## Spec contents

Pick guided by catalog below as relevant for specing the input artifact.
Intelligently constrain the reimplementation to a level of fidelity appropriate for the artifact.
Aim to make the reimplemented artifact a drop-in replacement for the original artifact was, without unduly constraining it on details that don't matter.

### Catalog

1. What it enables (capabilities)
   - Externally visible surfaces:
     - UI features, user stories enabled.
     - APIs, operations, data exchanges, events, query/reporting.
   - Input/output semantics; specify exact schemas/methods only where they matter - otherwise spec high level semantics
   - User stories and features critical to enabling them
2. Client-visible semantics and contracts
   - Stable identifiers; state models; idempotency; freshness/caching; ordering; concurrency
   - Authorization/roles if applicable.
3. Observability
   - Logs/metrics/traces and query surfaces required; wire log details; ...
4. Error model
   - Client-facing error behavior (invalid input, not found, timeout, ...)
   - Resilience behaviors (timeouts; malformed data; protocol errors; recovery; clean shutdown; ...)
5. Acceptance criteria (functional)
   - Externally verifiable outcomes for each capability/user story and failure mode; assertable by tests.
6. Acceptance criteria (non-functional)
   - As applicable: concurrency responsiveness; resource hygiene; robustness; observability sufficiency; security & accessibility; ...
7. Validation strategy
   - Unit or integration tests that check requirements at behavior level, possibly using mocked/fake dependencies if needed
   - Define scenarios asserting externally observable behavior.
   - Prefer automated tests over demo scripts.
8. Unspecified / left to implementer - list implementation details or unconstrained aspects as applicable:
   - Method names; payload shapes; module layout; concurrency framework choices; UI choices; ...
9. Definition of done
   - All tests + acceptance criteria pass; any required behaviors on errors

### Examples

- IPython notebook analyzing a specific Parquet blob:
  - Specify:
    - blob data shape, approx size if constraining (e.g. "fields: review ID, user ID (UUID), review text (str); 1.2 GB")
    - types of analysis, graphs (e.g.: "LLM-based sentiment analysis on review text, plot X = day (last 30),
      Y = avg sentiment, line color = company. 3 random representatives in each (positive, negative) x company bucket.
      For each (positive, negative) x company bucket, cluster LLM embeddings and summarize top 5 clusters.
      Export graphs on disk as PNG, representative samples as CSV including review + user IDs, sentiment score.")
    - any important inputs (e.g.: "download data from az://container/.../foo.parquet; use OpenAI API")
  - Omit unimportant details:
    - exact output filenames ("graph1.png")
    - implementation details where they don't matter (`df_raw_reviews` -> `df_reviews_with_sentiment` -> ...,
      "use `matplotlib` / `seaborn` / ...")
- Web app managing electronics parts:
  - Specify:
    - high-level stories/requirements (e.g.: "User may CRUD parts and update inventory. Parts track: name, part number,
      linked supplier parts, image, description, parameters - e.g., resistance in ohm, transistor type (PNP, NPN, ...).
      Parameters are a closed list that user may define and manage, including units and allowed values. ...")
    - crucial questions a dev implementing the app may have, including where a constraint is explicitly not needed (e.g.:
      "no need for multi-user / auth, single-user running on assumed-trusted localhost is fine")
    - APIs/UIs (e.g.: "REST API for basic inventory management - list all inventory items, update item (add/remove count);
      no need for API for other functions. UI: lightweight HTML + light modern JS framework, minimalist aesthetics;
      basic CLI to manage inventory: 'part-inventory list', '... get 74HC00', '... add/remove 74HC00 5'")
  - Omit unimportant details:
    - exact details of DB schema ("parts are stored in Postgres table `parts` with `id` (UUID), `name` (VARCHAR 50), ...")
    - which exact frameworks ("UI must be React+Tailwind...", "CLI must use Click", ...)
    - what is not important for this domain/application (e.g.: contextually this would be a light DIY-type single-user
      web app -> auth, load requriments, resilience etc. likely not important)
- Single `app/models.py` file from the same app:
  - Specify:
    - what would be needed to write a drop-in replacement - here, that _would_ include implemenetation details as
      a replacement `app/models.py` would have to fit within context of the rest of the app
    - e.g.: "write models for DB framework X on Postgres db. Tables: `parts` (`PartModel`) has fields `id` (UUID), ...,
      read provider configuration from `data/providers.yml`, schema: ..."
  - Omit:
    - Details that would be hidden behind the public interface of the file (i.e. implementation internals of classes/methods)

## Procedure (execution plan)

- Discover
  - Read the scoped code and documentation.
  - Make sure you cover all user-visible and external-facing features so you don't miss anything important - particularly UIs, APIs, etc.
  - Identify provided features, public/CLI/API surfaces, workflows, protocols, and artifacts.
- Distill
  - Abstract from code identifiers to behaviors.
  - Capture properties that are important for this type of artifact. As appropriate by artifact type (open-ended examples):
    - user stories served
    - provided features, UI affordances
    - crucial constraints/requirements
    - state machines
    - access control
- Interact
  - Ask the user clarifying questions directly.
  - Incorporate answers; if unanswered, proceed conservatively and note assumptions inline in output.
- Specify
  - Produce output Markdown file incorporating required elements.
  - Integrate user-provided supplemental instructions; omit any items the user ruled out.
- Validate
  - Ensure each acceptance criterion is testable without internal knowledge.
- Write the file
  - Write the file to a sensible location (e.g., the topmost scope directory).
- Report to user
  - Report the path of the file you wrote. State at high level what was included, what was omitted.
  - Suggest to user a couple concrete areas they may want to make spec tighter / looser. Example:

  **Assistant**:

  > I wrote the spec to /abs/path/SPEC.md. I included:
  >
  > - databases (4 options to support),
  > - rate limited external requests (GitHub, Coinbase) with retry+backoff,
  > - multi-user (admin/regular, OAuth2),
  > - CLI (Windows/Linux/macOS/FreeBSD/OpenBSD),
  > - alerting (platform-specific, email).
  >
  > Aspects we could adjust:
  >
  > 1. We require all 4 DB backends supported (SQLite, Postgres, MySQL, Firebase). Loosen: jany database (not explicitly listing each), or only one (say, SQLite)?
  > 2. Spec requires support for Windows, Linux, macOS, FreeBSD and OpenBSD. If OS compatibility is not important, loosen to drop list of required supported OSs?
  > 3. I specified requirement for currency conversion but left out our specific freshness algorithm. If freshness if important, I can tighten to "freshness 5 min or better"?
  >    ...

- Iterate to tighten / loosen the spec until it matches user intent. Example:

  **User**:

  > loosen 1 specific db does not matter; yes re 3 we need say better than 10 min freshness and resilience to any 1 curr conv provider going down
  > **Assistant**: _Edits spec_
  > I edited the spec to not list any specific database, just that data should be stored in a database.
  > Conversion specs also now say that the app must be resilient to any one currency provider going down, and that currency rates must be fresh within a 10 min window.
  > We now also require tests for this "provider goes down" resilience.
  >
  > More specs we could adjust to match the current implementation tighter/looser:
  >
  > 1. The requirement to support Windows, Linux, macOS, FreeBSD and OpenBSD.
  > 2. The spec does not prescribe the exact protocol to use with the notification API, treating it as an internal implementation detail.
  >    If you want to specify it exactly (e.g., so that reimplementation would be compatible with our notification handlers), I could bake it in.
  >    ...
  >    **User**:
  >    OK, this is about right, we're done.

## Style & guardrails

- Behavior over implementation; avoid internal names, credentials, or secrets.
- No large schemas or code excerpts; describe semantically.

## Example invocations

- "Compress :/project/foo into SPEC.md; exclude the legacy/ subtree; db must be Postgres; do not include caching; include admin UI for quotas; ensure UI is colorblind-friendly."
- "Compress /abs/path/service and /abs/path/ui; also expose metrics counters for failures and timeouts; exclude generated files (`*.gen.*`)."

## Success criteria

Output spec is self-contained, implementation-agnostic, and sufficient for a competent LLM to reimplement the system and to write tests that validate parity with the original behavior.
