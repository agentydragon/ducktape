local I = import '../../specimens/lib.libsonnet';

// iss-015: TypeScript types in channels.ts duplicate Python Pydantic models

I.issueOneOccurrence(
  rationale=|||
    The `channels.ts` file manually defines TypeScript types for WebSocket messages:

    ```typescript
    export type SessionMessage =
      | { type: 'session_snapshot'; session_state: any; run_state?: any }
      | { type: 'user_text'; text: string }
      | { type: 'assistant_text'; text: string }
      // ...

    export type McpMessage =
      | { type: 'mcp_snapshot'; sampling: any }
      // ...

    export type ApprovalsMessage =
      | { type: 'approvals_snapshot'; pending: any[] }
      // ...
    ```

    However, the codebase already has a Pydantic-to-TypeScript code generator at
    `adgn/scripts/generate_frontend_code.py` that:
    1. Uses `json-schema-to-typescript` to generate types from Pydantic models
    2. Generates type definitions to `adgn/agent/web/src/generated/types.ts`
    3. Is invoked via `npm run codegen`

    Manually maintaining parallel TypeScript types creates:
    - **Duplication**: Same data structures defined in both Python and TypeScript
    - **Drift risk**: Changes to Python models may not be reflected in TypeScript
    - **Maintenance burden**: Every schema change requires updates in two places

    Fix - add the corresponding Python Pydantic models to the code generator:

    1. **Find or create the Python models** for SessionMessage, McpMessage, ApprovalsMessage,
       PolicyMessage, UiMessage, ErrorMessage (likely in `adgn/agent/server/protocol.py` or
       similar location).

    2. **Add them to the generator** in `generate_frontend_code.py`:
       ```python
       from adgn.agent.server.protocol import (
           SessionMessage,
           McpMessage,
           ApprovalsMessage,
           PolicyMessage,
           UiMessage,
           ErrorMessage,
       )

       models_to_export = [
           # ... existing models ...
           SessionMessage,
           McpMessage,
           ApprovalsMessage,
           PolicyMessage,
           UiMessage,
           ErrorMessage,
       ]
       ```

    3. **Run the generator**: `npm run codegen`

    4. **Replace manual types** in channels.ts with imports from `generated/types.ts`:
       ```typescript
       import type {
         SessionMessage,
         McpMessage,
         ApprovalsMessage,
         PolicyMessage,
         UiMessage,
         ErrorMessage,
       } from '../../generated/types'
       ```

    5. **Keep only the envelope type** manually defined in channels.ts, as it's
       infrastructure code not a data model.
  |||,
  properties=['structured-data-over-untyped-mappings'],
  filesToRanges={
    'adgn/src/adgn/agent/web/src/features/chat/channels.ts': [
      [138, 174], // Manual TypeScript type definitions for all message types
    ],
  },
  gap_note=|||
    This pattern deserves a property like "single-source-of-truth-via-codegen": when
    data schemas are shared between languages/layers (e.g., Python backend and TypeScript
    frontend), they should be defined once in a canonical location and code-generated
    for other contexts, rather than manually duplicated. This is more specific than
    general "structured-data-over-untyped-mappings" as it addresses cross-language
    schema synchronization and build-time code generation as a quality practice.
  |||,
)
