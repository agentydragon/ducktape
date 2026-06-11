@README.md

Augur is pre-production. Do not add compatibility shims for older URL state
versions, request schemas, or serialized payloads unless the user explicitly
asks for backward compatibility.

The backend now executes through `augur/sim`; do not revive deleted
`augur/core` execution or market-bundle adapters. When extending API
responses, prefer native `ProjectionRun` read models
(`augur/sim/projections.py`) over deriving more tables from
`SimulationRun`'s long-form polars frames.
