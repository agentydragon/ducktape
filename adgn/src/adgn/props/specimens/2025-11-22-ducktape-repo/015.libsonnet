{
  title: 'TypeScript types in channels.ts duplicate Python Pydantic models',
  severity: 'moderate',
  category: 'maintainability',
  locations: [
    {
      path: 'adgn/src/adgn/agent/web/src/features/chat/channels.ts',
      lines: [138, 139, 140, 141, 142, 143, 144, 145, 146, 148, 149, 150, 151, 153, 154, 155, 156, 158, 159, 160, 161, 163, 164, 165, 166, 167, 169, 170, 171, 172, 173, 174],
      context: 'Manual TypeScript type definitions for channel messages',
    },
  ],
  description: |||
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
  |||,
  recommendation: |||
    Add the corresponding Python Pydantic models to the code generator:

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
}
